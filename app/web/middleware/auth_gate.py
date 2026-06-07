"""Auth gate — redirects un-authenticated requests to /landing.

Activation rule
---------------
The gate is OFF when the ``users`` table is empty (so a brand-new local
install still works without forcing the owner through signup). It flips
ON the first time any user signs up. Existing single-user installs that
never run signup keep working without changes.

The state is cached in a module-level boolean so the middleware doesn't
hit the DB on every request; we re-check the flag every 60 s and on
process start. After the first signup the cache flips ON within a
minute even without explicit cache invalidation.

Public allow-list
-----------------
Paths starting with these prefixes are always accessible:
  * /landing, /auth/*, /help, /static/*, /thumbs/*
  * /healthz, /api/health.json (so load balancers stay green)
  * /api/sync/*, /api/devices/heartbeat (agent-facing, auth via header)
  * /favicon.ico

Everything else needs ``persona_session`` cookie.
"""

from __future__ import annotations

import time
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from app.auth import SESSION_COOKIE_NAME, verify_session
from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.auth_gate")

# Cache window (seconds). After signup the gate activates within this.
_FLAG_TTL = 60.0

# Module-level cache of "is the gate active right now?"
_cache: dict[str, float | bool] = {"value": False, "checked_at": 0.0}

# Prefixes that bypass auth. Order matters only insofar as readability.
_PUBLIC_PREFIXES: tuple[str, ...] = (
    "/landing",
    "/auth/",
    "/help",
    "/static/",
    "/thumbs/",
    "/healthz",
    "/api/health.json",
    "/api/sync/",
    "/api/devices/heartbeat",
    # T16 (2026-06-07) — iOS Shortcut hits these with X-Device-Token,
    # not a cookie session. The route itself enforces auth.
    "/api/ingest/",
    # T18 — installer.sh fetch happens from user's Mac terminal where
    # there's no session cookie. Single-use ``t`` query token is the auth.
    "/api/install/",
    "/favicon.ico",
)


async def _gate_active() -> bool:
    """Return whether the gate should redirect un-authenticated requests."""
    now = time.monotonic()
    if now - float(_cache["checked_at"]) < _FLAG_TTL:
        return bool(_cache["value"])
    try:
        async with get_connection() as conn:
            cursor = await conn.execute("SELECT 1 FROM users LIMIT 1")
            row = await cursor.fetchone()
        active = row is not None
    except Exception as exc:
        # DB hiccup — fail-open (gate inactive) so a transient SQLite
        # lock never bricks the whole site.
        log.warning("auth_gate.check_failed", error=str(exc))
        active = False
    _cache["value"] = active
    _cache["checked_at"] = now
    return active


def _is_public_path(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


class AuthGateMiddleware(BaseHTTPMiddleware):
    """Redirect un-authenticated visitors to /landing once any user exists."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Response]
    ) -> Response:
        path = request.url.path

        if _is_public_path(path):
            return await call_next(request)

        if not await _gate_active():
            return await call_next(request)

        token = request.cookies.get(SESSION_COOKIE_NAME)
        session = await verify_session(token) if token else None
        if session is not None:
            return await call_next(request)

        # Browser nav → 303 to /landing. JSON / agent endpoints get 401
        # so they don't end up with HTML in their response body.
        if path.startswith("/api/"):
            return Response(
                content='{"detail":"authentication required"}',
                status_code=401,
                media_type="application/json",
            )
        return RedirectResponse(url="/landing", status_code=303)
