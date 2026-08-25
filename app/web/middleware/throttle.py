"""Per-user throttling for expensive member-reachable endpoints.

Before this, the *only* rate limiting in Persona was per-IP on ``/auth/*``.
Once ``owner_exclusive_mode`` flips to ``0``, every registered stranger can
reach the endpoints that cost real money and real CPU:

* ``/api/chat/*`` — LLM generation (the PC worker / Ollama box);
* ``/api/copilot`` — LLM generation, SSE-streamed;
* ``/api/messages/*/ai`` — LLM drafts inside direct messages;
* ``/api/ask``, ``/api/voice-search``, ``/api/journal/voice-dictate`` — more LLM;
* ``/api/friends/*`` — social search over every discoverable account;
* ``/api/skills*`` — skill install (network fetch + disk write).

Design
------
Reuses :func:`app.web.rate_limit.allow` (in-process sliding window, no Redis,
no new dependency). The key is the **user id** when the request is
authenticated — a per-IP limit is close to worthless against a logged-in
attacker on a phone tether — and falls back to the client IP for the rare
unauthenticated case.

Placement: this middleware must sit *inside* :class:`AuthGateMiddleware` so
that ``request.state.user_id`` / ``request.state.is_owner`` are already
populated. It never performs its own session lookup: an extra DB round-trip on
every request is exactly the kind of cost a throttle is supposed to prevent.

Owner exemption
---------------
The owner is **exempt**. This is a self-hosted product whose owner runs
batch jobs, dream cycles and nightly builds against his own instance; throttling
him into his own box is a worse failure than the abuse this guards against.
Members get the configured ceiling; anonymous callers get the member ceiling
keyed by IP.

Configuration (kv, 60 s cache)
------------------------------
============================  =======  ==================================
kv key                        default  meaning
============================  =======  ==================================
``throttle_enabled``          ``1``    master switch; ``0`` disables all
``throttle_llm_per_5min``     ``40``   LLM-backed calls per user / 5 min
``throttle_social_per_min``   ``60``   social + search calls per user/min
``throttle_skills_per_hour``  ``20``   skill installs per user / hour
``throttle_write_per_min``    ``120``  any other member POST/PUT/DELETE
============================  =======  ==================================

Accounts that never proved control of their email address (see
:mod:`app.auth.verification`) get ``limit // UNVERIFIED_BUDGET_DIVISOR``
(currently half), minimum 1, of every budget above. Anonymous callers are
treated the same way. That penalty applies **only on an instance that can
actually send mail** — verification is recorded by following a link from an
inbox, so where no SMTP relay is configured nobody can ever become verified
and halving everyone's budget would punish users for something impossible.
:func:`app.auth.verification.unverified_penalty_applies` makes that call.

A malformed or missing value falls back to the default — the limiter keeps
limiting. A kv/DB failure does the same. There is no code path where an error
turns the throttle off (fail safe = keep denying), only the explicit
``throttle_enabled=0`` switch does that.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse

from app.auth.verification import UNVERIFIED_BUDGET_DIVISOR, unverified_penalty_applies
from app.logging_setup import get_logger
from app.web.rate_limit import allow as _rate_allow

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response

log = get_logger("persona.throttle")

__all__ = ["BUCKETS", "ThrottleMiddleware", "reset_cache", "reset_counters", "reset_state"]

#: Prefix every key this middleware writes into :mod:`app.web.rate_limit` uses,
#: so :func:`reset_counters` can clear exactly its own state and nothing else.
KEY_PREFIX = "thr:"


class _Bucket:
    """One throttle class: what it matches, its kv key, budget and window.

    Matching is intentionally two-part. ``prefixes`` catches whole endpoint
    families; ``suffixes`` catches the *verb* at the end of a parameterised
    path (``/api/chat/sessions/{id}/send``) — the expensive action lives there,
    while its siblings (``/rename``, ``/rate``, ``/messages``) are cheap CRUD
    that must NOT eat the LLM budget. Throttling all of ``/api/chat/`` as if it
    were generation is how a rate limiter ends up blocking someone from
    *reading* their own chat history.
    """

    __slots__ = (
        "name",
        "prefixes",
        "suffixes",
        "kv_key",
        "default_max",
        "window",
        "methods",
    )

    def __init__(
        self,
        name: str,
        prefixes: tuple[str, ...],
        kv_key: str,
        default_max: int,
        window: int,
        methods: frozenset[str] | None = None,
        suffixes: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.name = name
        self.prefixes = prefixes
        self.suffixes = suffixes  # (path-prefix, path-suffix) pairs
        self.kv_key = kv_key
        self.default_max = default_max
        self.window = window
        self.methods = methods

    def matches(self, path: str, method: str) -> bool:
        if self.methods is not None and method not in self.methods:
            return False
        if any(path == p or path.startswith(p) for p in self.prefixes):
            return True
        return any(
            path.startswith(prefix) and path.endswith(suffix)
            for prefix, suffix in self.suffixes
        )


_UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Ordered — first match wins, so the specific LLM buckets precede the
#: catch-all write bucket.
BUCKETS: tuple[_Bucket, ...] = (
    _Bucket(
        # Endpoints that actually invoke a model. Everything here costs GPU
        # seconds on the owner's PC worker (or tokens on his API key).
        "llm",
        (
            "/api/copilot",
            "/api/ask",
            "/api/voice-search",
            "/api/journal/voice-dictate",
            "/api/day/ask",
            "/api/expand",
            "/api/chat/compare",
            "/api/chat/auto-prompt",
        ),
        "throttle_llm_per_5min",
        40,
        300,
        methods=_UNSAFE,
        suffixes=(
            ("/api/chat/sessions/", "/send"),
            ("/api/chat/sessions/", "/send-stream"),
            ("/api/chat/sessions/", "/build"),
            # ИИ-черновик в личных сообщениях.
            ("/api/messages/", "/ai"),
        ),
    ),
    _Bucket(
        "skills",
        ("/api/skills",),
        "throttle_skills_per_hour",
        20,
        3600,
        methods=_UNSAFE,
    ),
    _Bucket(
        # Поиск по людям и переписке: дёшево по CPU, но это единственная
        # поверхность, где один аккаунт перебирает ЧУЖИЕ (скан всех
        # discoverable-профилей), поэтому потолок нужен.
        "social",
        ("/api/friends", "/api/settings/search", "/api/palette.json"),
        "throttle_social_per_min",
        60,
        60,
    ),
    _Bucket(
        "write",
        ("/api/",),
        "throttle_write_per_min",
        120,
        60,
        methods=_UNSAFE,
    ),
)

_MSG = (
    "Слишком часто. Переведи дух на минуту — и продолжим. "
    "Это ограничение защищает общий сервер от перегрузки."
)

_KV_TTL = 60.0
_cache: dict[str, object] = {"values": None, "checked_at": 0.0}


def reset_cache() -> None:
    """Drop the cached kv budgets (tests / after a settings change)."""
    _cache["values"] = None
    _cache["checked_at"] = 0.0


def reset_counters() -> None:
    """Forget every request this middleware has counted.

    The sliding windows live in :mod:`app.web.rate_limit`, whose store is a
    module-level dict — process-global by design (no Redis, no table). Each
    test gets a fresh *database* but shares the interpreter, so without an
    explicit reset a user who made N calls in one test starts the next one
    already near the ceiling: three tests in ``tests/test_mvp_smoke_audit.py``
    passed in isolation and failed when the file ran whole. That is a flake
    disguised as a product bug, so the hook is public and wired into the
    autouse fixture in ``tests/conftest.py``.

    Only keys this middleware owns (``thr:``) are removed — the auth routes'
    per-IP counters live in the same store and are cleared separately.
    """
    from app.web.rate_limit import _EVENTS  # noqa: PLC0415 — test/ops hook only

    for key in [k for k in _EVENTS if k.startswith(KEY_PREFIX)]:
        _EVENTS.pop(key, None)


def reset_state() -> None:
    """Clear both the kv budget cache and the counters. One call for tests."""
    reset_cache()
    reset_counters()


def _defaults() -> dict[str, int]:
    values = {b.kv_key: b.default_max for b in BUCKETS}
    values["throttle_enabled"] = 1
    return values


async def _budgets() -> dict[str, int]:
    """Resolve kv budgets with a 60 s cache. Any failure → defaults."""
    now = time.monotonic()
    cached = _cache["values"]
    if cached is not None and now - float(_cache["checked_at"]) < _KV_TTL:  # type: ignore[arg-type]
        return cached  # type: ignore[return-value]
    values = _defaults()
    try:
        from app.storage.db import get_connection  # noqa: PLC0415
        from app.storage.repository import get_kv  # noqa: PLC0415

        async with get_connection() as conn:
            for key in list(values):
                raw = await get_kv(conn, key)
                if raw is None:
                    continue
                text = str(raw).strip()
                if key == "throttle_enabled":
                    values[key] = 0 if text in {"0", "off", "false", "no"} else 1
                elif text.isdigit() and int(text) > 0:
                    values[key] = int(text)
    except Exception as exc:  # noqa: BLE001 — config failure keeps the defaults
        log.debug("throttle.kv_read_failed", error=str(exc))
        values = _defaults()
    _cache["values"] = values
    _cache["checked_at"] = now
    return values


def _client_key(request: Request) -> tuple[str, bool]:
    """Return ``(bucket-key, is_owner)`` for this request.

    Prefers the authenticated user id (set by :class:`AuthGateMiddleware`);
    falls back to the peer address. Never trusts ``X-Forwarded-For`` here —
    the auth-route limiter owns that decision, and a throttle that can be
    bypassed with a header is not a throttle.
    """
    uid = getattr(request.state, "user_id", None)
    is_owner = bool(getattr(request.state, "is_owner", False))
    if uid is not None:
        return f"u{int(uid)}", is_owner
    peer = request.client.host if request.client else "unknown"
    return f"ip{peer}", False


def _too_many(request: Request) -> Response:
    if request.url.path.startswith("/api/"):
        return JSONResponse(
            {"ok": False, "error": _MSG, "detail": "rate_limited"},
            status_code=429,
            headers={"Retry-After": "60"},
        )
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<body style='font-family:system-ui,sans-serif;background:#0b0b0f;color:#eee;"
        f"padding:3rem;text-align:center'><h2>⏳ {_MSG}</h2></body>",
        status_code=429,
        headers={"Retry-After": "60"},
    )


class ThrottleMiddleware(BaseHTTPMiddleware):
    """Per-user sliding-window limits on the expensive member surface."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        # Fast path: only /api/* is ever throttled here. Page loads are cheap
        # and throttling a navigation produces a baffling user experience.
        if not path.startswith("/api/"):
            return await call_next(request)

        bucket = next((b for b in BUCKETS if b.matches(path, request.method)), None)
        if bucket is None:
            return await call_next(request)

        key, is_owner = _client_key(request)
        if is_owner:
            # The owner is never throttled out of his own instance.
            return await call_next(request)

        budgets = await _budgets()
        if not budgets.get("throttle_enabled", 1):
            return await call_next(request)
        limit = int(budgets.get(bucket.kv_key, bucket.default_max))

        # Accounts that never proved control of their email address get a
        # fraction of the budget (see app/auth/verification.py). Anonymous
        # callers are treated as unverified for the same reason. On an
        # instance that cannot send mail at all, verification is unreachable
        # and the penalty is skipped entirely — full budget for everyone.
        uid = getattr(request.state, "user_id", None)
        if await unverified_penalty_applies(uid):
            limit = max(1, limit // UNVERIFIED_BUDGET_DIVISOR)

        if not _rate_allow(f"thr:{bucket.name}:{key}", limit, bucket.window):
            log.info(
                "throttle.rejected",
                bucket=bucket.name,
                subject=key,
                limit=limit,
                window=bucket.window,
                path=path,
            )
            return _too_many(request)
        return await call_next(request)
