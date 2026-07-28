"""First-run redirect gate — bounces fresh installs to ``/setup`` (v0.50).

The gate is a Starlette ``BaseHTTPMiddleware`` so it lives outside the
APIRouter system entirely. It fires before any route handler runs, looks
at one kv row (``setup_complete``) and either lets the request through
(setup done, or path on the allow-list) or returns a 303 ``Location:
/setup`` redirect.

The allow-list is intentionally tiny:

    * ``/setup`` and its POST target — the wizard itself must always be
      reachable, otherwise the redirect would loop.
    * ``/static/*`` — Tailwind CSS, htmx, Alpine and every icon the
      wizard template renders. Without this carve-out the wizard would
      load as unstyled HTML on the very first visit.
    * ``/api/*`` — programmatic clients (the bookmarklet, the chrome
      extension, ``capture_api``) must not be tricked into POSTing form
      data to the wizard. A 303 here would break JSON-only consumers.
    * ``/favicon.ico`` — every browser asks for it on first paint and
      a redirect-to-HTML answer here logs an ugly mime-type warning.
    * ``/events`` — the SSE stream used by ``base.html`` for the live
      capture pill. Redirecting an EventSource breaks the connection
      and burns a reconnect-backoff on the client.
    * ``/healthz`` — cheap process liveness used by external monitors.
    * ``/health`` — compatibility diagnostics; must keep a stable HTTP
      contract regardless of onboarding state.

We resolve the ``setup_complete`` flag synchronously via a short-lived
stdlib ``sqlite3`` connection (same trick :mod:`app.web.templates_engine`
uses for the theme lookup) — the middleware path runs inside Starlette's
async dispatch but we do not want to await an aiosqlite pool here when
the value is essentially cached forever after first save. We do, however,
cache the result in-process once it turns True: SQLite is cheap but the
gate runs on every single request and a hit-rate of 100% deserves to
skip the syscall entirely.
"""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Final

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import RedirectResponse

from app.logging_setup import get_logger
from app.settings import get_settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response

log = get_logger("persona.setup")

# Exact paths that bypass the gate. Anything not in here AND not in
# ``_ALLOW_PREFIXES`` and not the wizard itself gets redirected.
_ALLOW_EXACT: Final[frozenset[str]] = frozenset(
    {
        "/setup",
        "/favicon.ico",
        "/events",
        "/healthz",
        "/health",
    },
)

# Prefix-matched carve-outs. Kept tiny and explicit — every new entry
# widens the "can hit Persona before configuring it" surface.
_ALLOW_PREFIXES: Final[tuple[str, ...]] = (
    "/static/",
    "/api/",
)

_KV_SETUP_COMPLETE: Final[str] = "setup_complete"


class _CompletionCache:
    """Tiny one-bit memoiser for the ``setup_complete`` row.

    Once the flag flips True (the wizard was completed) the middleware
    short-circuits every subsequent request without touching SQLite. We
    don't bother invalidating because the flag is monotonic — Persona
    has no "un-complete setup" operation, and even if a row were
    manually deleted the worst case is a server restart away from
    re-prompting onboarding.
    """

    def __init__(self) -> None:
        self._done = False

    def is_done(self) -> bool:
        return self._done

    def mark_done(self) -> None:
        self._done = True


_cache = _CompletionCache()


def _read_flag_sync() -> bool:
    """Read the ``setup_complete`` kv row via a short-lived stdlib conn.

    Returns False on any failure (missing DB, missing table, corrupt
    value) — a redirect to the wizard is the *safer* default than
    silently letting the user onto a half-configured timeline.
    """
    db_path = get_settings().db_path
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.execute(
                "SELECT value FROM kv_settings WHERE key = ?",
                (_KV_SETUP_COMPLETE,),
            )
            row = cursor.fetchone()
    except sqlite3.Error as exc:
        # First boot: the database may not even exist yet (lifespan
        # runs ``init_database`` before serving requests, so this is
        # rare — but we still don't want the middleware to 500).
        log.debug("setup.gate.db_unavailable", error=str(exc))
        return False
    if row is None:
        return False
    return str(row[0]) == "true"


def _is_allowed(path: str) -> bool:
    """Return True iff ``path`` may bypass the gate regardless of state."""
    if path in _ALLOW_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in _ALLOW_PREFIXES)


class SetupGateMiddleware(BaseHTTPMiddleware):
    """Redirect unconfigured installs to the setup wizard.

    Wire it into the FastAPI app via
    ``app.add_middleware(SetupGateMiddleware)`` *after* the routers are
    mounted; ordering relative to other middlewares does not matter
    because this gate only emits 303s on the html path and lets
    everything else through.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path

        # Fast path #1: setup is done — we cache the bit in-process so
        # 99.9999% of requests skip the SQLite roundtrip.
        if _cache.is_done():
            return await call_next(request)

        # Fast path #2: whitelisted path. Save the kv read for paths we
        # would let through anyway.
        if _is_allowed(path):
            return await call_next(request)

        # Cold path: hit SQLite once. If the flag is set we update the
        # cache *and* let this very request through — we don't want the
        # first request after save to still get redirected.
        if _read_flag_sync():
            _cache.mark_done()
            return await call_next(request)

        # Not done, not allow-listed → bounce to the wizard. 303 keeps
        # any in-flight POST from being replayed against /setup.
        log.info("setup.gate.redirect", path=path)
        return RedirectResponse(url="/setup", status_code=303)


__all__ = ["SetupGateMiddleware"]
