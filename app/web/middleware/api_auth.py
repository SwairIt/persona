"""Bearer-token middleware for ``/api/*`` endpoints (v0.34).

Wire it into the FastAPI app via ``app.add_middleware(ApiAuthMiddleware)``.
The middleware only inspects requests whose path starts with ``/api/`` —
everything else is passed straight through, which keeps the HTML UI, the
static assets and SSE streams unaffected.

Behaviour matrix
----------------

================================  =====================================
Request                           Outcome
================================  =====================================
no ``Authorization`` header       pass through (default) **or** 401 if
                                  ``settings.api_auth_required`` is True
``Authorization: Bearer <good>``  ``request.state.scopes`` populated,
                                  ``last_used_at`` bumped, pass through
``Authorization: Bearer <bad>``   401 JSON with ``reason`` from
                                  :func:`verify_token`
malformed ``Authorization``       401 JSON with reason ``"malformed"``
================================  =====================================

The middleware never logs the raw token; only the *reason* a request
was rejected and (on success) the resolved token id are visible in
structlog under the ``persona.api_tokens`` logger.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.api_tokens import verify_token
from app.logging_setup import get_logger
from app.settings import get_settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from starlette.requests import Request
    from starlette.responses import Response

log = get_logger("persona.api_tokens")

_API_PREFIX = "/api/"
_BEARER_PREFIX = "Bearer "


def _unauthorized(reason: str) -> JSONResponse:
    """Render the canonical 401 body used for every rejection path."""
    return JSONResponse(
        {"error": "unauthorized", "reason": reason},
        status_code=401,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _extract_bearer(header: str) -> str | None:
    """Pull the raw token out of ``Authorization``. ``None`` if malformed.

    Empty header *is* malformed at this layer — the caller decides
    whether "no header" means anonymous or 401 *before* invoking us.
    """
    if not header.startswith(_BEARER_PREFIX):
        return None
    raw = header[len(_BEARER_PREFIX) :].strip()
    return raw or None


class ApiAuthMiddleware(BaseHTTPMiddleware):
    """Gate ``/api/*`` on a bearer token when one is supplied (or required)."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Fast path: anything outside ``/api/`` is never gated.
        if not request.url.path.startswith(_API_PREFIX):
            return await call_next(request)

        settings = get_settings()
        header = request.headers.get("authorization", "").strip()

        # No header at all → only fail when the operator opted in to
        # mandatory auth. Default is "open" so existing localhost UI
        # calls keep working without code changes.
        if not header:
            if settings.api_auth_required:
                log.info("api_token.middleware.reject", reason="missing_header")
                return _unauthorized("missing_token")
            return await call_next(request)

        # Header present but not a Bearer scheme, or empty payload →
        # always reject; we don't want Basic / Digest / garbage to
        # silently be treated as anonymous.
        raw = _extract_bearer(header)
        if raw is None:
            log.info("api_token.middleware.reject", reason="malformed")
            return _unauthorized("malformed")

        result = await verify_token(raw)
        if not result.get("ok"):
            return _unauthorized(result.get("reason", "invalid"))

        # Stash the resolved scopes + token id on ``request.state`` so
        # downstream route handlers can authorise individual operations
        # without re-parsing the header.
        request.state.api_token_id = result.get("id")
        request.state.scopes = result.get("scopes", "read")
        return await call_next(request)
