"""FastAPI dependencies for reading the current user from a request.

Two flavours:
    * ``current_user_optional`` — returns the session record or ``None``;
      use for pages that render both signed-in and signed-out states.
    * ``current_user_required`` — raises 401 redirect to /auth/login;
      use for pages that must not be public.

Pulls the token from the ``persona_session`` cookie. Routes never receive
the raw token — only the resolved session record. The dependency itself
does the DB lookup so the route stays trivial.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from fastapi.responses import RedirectResponse

from app.auth.sessions import SESSION_COOKIE_NAME, SessionRecord, verify_session


async def current_user_optional(request: Request) -> SessionRecord | None:
    """Return the session record for the request, or ``None`` if absent."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    return await verify_session(token)


async def current_user_required(request: Request) -> SessionRecord:
    """Return the session record or raise 303 redirect to /auth/login.

    We raise ``HTTPException`` with a 303 status so FastAPI surfaces it
    to the browser as a navigation, rather than a JSON error page.
    """
    session = await current_user_optional(request)
    if session is None:
        # 303 with a Location header sends the browser to /auth/login.
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/auth/login"},
        )
    return session


def redirect_to_login() -> RedirectResponse:
    """Convenience helper for non-dependency code paths."""
    return RedirectResponse(url="/auth/login", status_code=status.HTTP_303_SEE_OTHER)
