"""Email-verification state, without a schema migration.

Recommendation implemented here
-------------------------------
**Do not block usage behind verification.** Persona's registration flow
(``POST /auth/register``) creates the account, shows the generated password on
screen and logs the person straight in — that is deliberate, and it is the only
path that works on a server with no SMTP configured. Gating the product behind
"click the link in your email" would brick registration on exactly the
deployments that cannot send email, which is a worse outcome than an unverified
account existing.

Instead: an unverified account is **marked** and **throttled harder**. Verified
accounts get the normal budgets; unverified ones get a fraction of them (see
:data:`UNVERIFIED_BUDGET_DIVISOR` and its use in
``app.web.middleware.throttle``). Cost to a legitimate user who never opens
their mail: a slower ceiling on LLM calls. Cost to someone farming throwaway
accounts to burn the owner's GPU: the same work now needs a real mailbox per
account.

Storage
-------
kv row ``email_verified_<user_id>`` = ``"1"``. No migration, no new column, and
it is trivially inspectable/settable by the owner. Verification is recorded
when a **magic link is consumed** (``/auth/magic/{token}``) — following a link
delivered to the address *is* proof of control of that mailbox, so no separate
"verify your email" flow, template or route is needed. A password-reset link
counts too, for the same reason.

Only where the mailbox is reachable
-----------------------------------
The penalty is conditional on the *instance* being able to send mail at all
(:func:`mail_deliverable`). Verification here is only ever recorded by
following a link that arrived in an inbox, so on a box with no SMTP relay
nobody can become verified, ever — halving everyone's budget there punishes
users for something physically impossible and looks exactly like the product
being broken. When mail cannot be delivered, every account is treated as
verified for throttling purposes; when it can, an unverified account pays the
usual half. See :func:`unverified_penalty_applies`.

Caching
-------
The flag is read on the request hot path (throttling), so results are cached
per user for :data:`_TTL` seconds, capped at :data:`_MAX_CACHED` entries.
A DB failure resolves to **unverified** (the stricter budget), never to
verified — an error must not hand out the higher ceiling. Deliverability is
one instance-wide answer and is cached separately (:data:`_MAIL_TTL`).
"""

from __future__ import annotations

import time

from app.logging_setup import get_logger

log = get_logger("persona.auth.verification")

__all__ = [
    "UNVERIFIED_BUDGET_DIVISOR",
    "is_verified",
    "kv_key",
    "mail_deliverable",
    "mark_verified",
    "reset_cache",
    "reset_mail_cache",
    "unverified_penalty_applies",
]

#: Unverified accounts get ``limit // divisor`` (minimum 1) of every budget.
#: Deliberately 2, not 4: a deployment whose SMTP is not configured cannot let
#: *anyone* verify (registration itself says so on screen), and quartering the
#: budget for every user on such a box would look like the product being
#: broken. Halving still doubles the cost of farming throwaway accounts.
UNVERIFIED_BUDGET_DIVISOR = 2

_TTL = 300.0
_MAX_CACHED = 1024
_cache: dict[int, tuple[bool, float]] = {}

#: Instance-wide "can we send mail?" answer. Longer TTL than the per-user flag
#: would buy little (SMTP settings change roughly never) and a stale *false*
#: only ever means someone keeps the full budget for another five minutes.
_MAIL_TTL = 300.0
#: A one-slot dict rather than a module-level scalar, so nothing here needs
#: ``global`` (same idiom as ``app.web.middleware.throttle._cache``).
_mail_cache: dict[str, float | bool | None] = {"value": None, "checked_at": 0.0}


def kv_key(user_id: int) -> str:
    return f"email_verified_{int(user_id)}"


def reset_cache() -> None:
    """Drop the cached state (tests / after marking someone verified)."""
    _cache.clear()
    reset_mail_cache()


def reset_mail_cache() -> None:
    """Forget whether mail is deliverable (called after /settings/smtp saves)."""
    _mail_cache["value"] = None
    _mail_cache["checked_at"] = 0.0


async def mark_verified(user_id: int | None) -> None:
    """Record that ``user_id`` proved control of their email address."""
    if user_id is None:
        return
    try:
        from app.storage.db import get_connection  # noqa: PLC0415
        from app.storage.repository import set_kv  # noqa: PLC0415

        async with get_connection() as conn:
            await set_kv(conn, kv_key(int(user_id)), "1")
    except Exception as exc:  # noqa: BLE001 — never break a login on bookkeeping
        log.warning("auth.verification.mark_failed", error=str(exc))
        return
    _cache[int(user_id)] = (True, time.monotonic())
    log.info("auth.verification.marked", user_id=int(user_id))


async def is_verified(user_id: int | None) -> bool:
    """True when the account has proved control of its email. Errors → False."""
    if user_id is None:
        return False
    uid = int(user_id)
    now = time.monotonic()
    cached = _cache.get(uid)
    if cached is not None and now - cached[1] < _TTL:
        return cached[0]
    verified = False
    try:
        from app.storage.db import get_connection  # noqa: PLC0415
        from app.storage.repository import get_kv  # noqa: PLC0415

        async with get_connection() as conn:
            raw = await get_kv(conn, kv_key(uid))
        verified = str(raw or "").strip() == "1"
    except Exception as exc:  # noqa: BLE001 — unknown means "not verified"
        log.debug("auth.verification.read_failed", error=str(exc))
        return False
    if len(_cache) >= _MAX_CACHED:
        _cache.clear()
    _cache[uid] = (verified, now)
    return verified


async def mail_deliverable() -> bool:
    """True when this instance can actually deliver a verification email.

    Source of truth is :func:`app.smtp_delivery.delivery_status` — the exact
    configuration resolution ``send_email`` performs (kv wins, ``.env`` is the
    fallback), so there is no second opinion about what "configured" means.
    Only ``"ok"`` counts: ``smtp_enabled='true'`` with an empty ``smtp_host``
    resolves to ``"misconfigured"`` and is NOT deliverable, which is precisely
    the state prod was found in.

    **Errors resolve to False (= no penalty), on purpose.** That is the
    opposite of :func:`is_verified`'s fail-closed rule, and it is deliberate:
    the unverified penalty is an anti-abuse *nicety* (it makes farming
    throwaway accounts cost a real mailbox), not an access-control boundary.
    Nothing is authorised by this answer — the configured throttle ceilings,
    the auth gate, the owner-exclusive mode and the per-IP auth limiter all
    still apply in full. Halving a paying member's budget because a kv read
    hiccuped is a real, visible defect; letting an abuser have the ordinary
    (already limited) budget during that hiccup is not.

    Cached for :data:`_MAIL_TTL` seconds — this sits on the request hot path.
    """
    now = time.monotonic()
    cached = _mail_cache["value"]
    if cached is not None and now - float(_mail_cache["checked_at"] or 0.0) < _MAIL_TTL:
        return bool(cached)
    try:
        from app.smtp_delivery import delivery_status  # noqa: PLC0415

        deliverable = await delivery_status() == "ok"
    except Exception as exc:  # noqa: BLE001 — see docstring: user-friendly side
        log.debug("auth.verification.mail_probe_failed", error=str(exc))
        return False
    _mail_cache["value"] = deliverable
    _mail_cache["checked_at"] = now
    return deliverable


async def unverified_penalty_applies(user_id: int | None) -> bool:
    """Should this caller get the reduced budget?

    Only when the instance can actually deliver mail *and* the account never
    proved control of its address. On a mail-less instance every account —
    including the anonymous ``None`` — gets the full budget, because becoming
    verified there is impossible rather than merely neglected.
    """
    if not await mail_deliverable():
        return False
    return not await is_verified(user_id)
