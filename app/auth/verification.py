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

Caching
-------
The flag is read on the request hot path (throttling), so results are cached
per user for :data:`_TTL` seconds, capped at :data:`_MAX_CACHED` entries.
A DB failure resolves to **unverified** (the stricter budget), never to
verified — an error must not hand out the higher ceiling.
"""

from __future__ import annotations

import time

from app.logging_setup import get_logger

log = get_logger("persona.auth.verification")

__all__ = [
    "UNVERIFIED_BUDGET_DIVISOR",
    "is_verified",
    "kv_key",
    "mark_verified",
    "reset_cache",
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


def kv_key(user_id: int) -> str:
    return f"email_verified_{int(user_id)}"


def reset_cache() -> None:
    """Drop the per-user cache (tests / after marking someone verified)."""
    _cache.clear()


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
