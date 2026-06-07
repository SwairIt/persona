"""Authentication routes: signup, login, logout.

Three GET pages render the forms; three POST handlers process them.
Sessions live in an HTTP-only cookie set on the redirect response.

Cookie security flags:
    * ``HttpOnly``  — JS can't read it (no XSS theft).
    * ``SameSite=Lax`` — protects against most CSRF without breaking
      OAuth-style cross-site redirects.
    * ``Secure`` — only set when the request came in over HTTPS.
      Localhost dev over http:// gets a non-secure cookie so the form
      still works without a TLS terminator.

The signup form is intentionally minimal: email, password, optional
display name. Multi-device data live in a separate ``device`` table
(planned T3) and don't belong on the signup form.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import (
    SESSION_COOKIE_NAME,
    authenticate,
    create_user,
    current_user_optional,
    issue_session,
    revoke_session,
)
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["auth"])
log = get_logger("persona.auth.routes")

# Cap on the User-Agent we persist into auth_session. The longest UAs
# we've seen are around 200 chars; anything beyond is junk.
_MAX_UA_LEN = 250


def _set_session_cookie(
    response: Response, token: str, secure: bool, max_age_seconds: int
) -> None:
    """Set the session cookie with the right security flags."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=max_age_seconds,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def _trim_ua(raw: str | None) -> str | None:
    if not raw:
        return None
    return raw[:_MAX_UA_LEN]


# --- GET pages -------------------------------------------------------------


@router.get("/auth/signup", response_class=HTMLResponse, response_model=None)
async def signup_page(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> HTMLResponse | RedirectResponse:
    """Render the signup form, or bounce to /now when already signed in."""
    if session is not None:
        return RedirectResponse(url="/now", status_code=303)
    return templates.TemplateResponse(
        request,
        "auth_signup.html",
        {
            "title": "Создать аккаунт",
            "active_nav": "",
            "error": None,
            "email": "",
        },
    )


@router.get("/auth/login", response_class=HTMLResponse, response_model=None)
async def login_page(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> HTMLResponse | RedirectResponse:
    """Render the login form, or bounce to /now when already signed in."""
    if session is not None:
        return RedirectResponse(url="/now", status_code=303)
    return templates.TemplateResponse(
        request,
        "auth_login.html",
        {
            "title": "Войти",
            "active_nav": "",
            "error": None,
            "email": "",
        },
    )


# --- POST handlers ---------------------------------------------------------


@router.post("/auth/signup", response_class=HTMLResponse, response_model=None)
async def signup_submit(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    display_name: Annotated[str, Form()] = "",
) -> Response:
    """Create the user, start a session, redirect to /now."""
    try:
        user = await create_user(email, password, display_name)
    except ValueError as exc:
        raw = str(exc)
        # Translate the few known error keys; anything else is shown
        # verbatim (catches future validator additions without losing info).
        ru = {
            "email already registered": "Этот email уже зарегистрирован.",
            "invalid email": "Неверный формат email.",
            "password must be at least 8 characters": "Пароль должен быть минимум 8 символов.",
        }.get(raw, raw)
        return templates.TemplateResponse(
            request,
            "auth_signup.html",
            {
                "title": "Создать аккаунт",
                "active_nav": "",
                "error": ru,
                "email": email,
            },
            status_code=400,
        )
    ua = _trim_ua(request.headers.get("user-agent"))
    token, _expires_at = await issue_session(user["id"], user_agent=ua)
    response = RedirectResponse(url="/now", status_code=303)
    secure = request.url.scheme == "https"
    _set_session_cookie(response, token, secure, 30 * 24 * 3600)
    return response


@router.post("/auth/login", response_class=HTMLResponse, response_model=None)
async def login_submit(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    """Verify credentials, start a session, redirect to /now."""
    user = await authenticate(email, password)
    if user is None:
        return templates.TemplateResponse(
            request,
            "auth_login.html",
            {
                "title": "Войти",
                "active_nav": "",
                "error": "Неверный email или пароль.",
                "email": email,
            },
            status_code=401,
        )
    ua = _trim_ua(request.headers.get("user-agent"))
    token, _expires_at = await issue_session(user["id"], user_agent=ua)
    response = RedirectResponse(url="/now", status_code=303)
    secure = request.url.scheme == "https"
    _set_session_cookie(response, token, secure, 30 * 24 * 3600)
    return response


@router.post("/auth/logout")
async def logout_submit(request: Request) -> RedirectResponse:
    """Revoke the current session and clear the cookie."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        await revoke_session(token)
    response = RedirectResponse(url="/landing", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


# GET version of logout for convenience hamburger-menu links — turns into
# a fast POST behind the scenes by going to /auth/logout via fetch in the
# client, but for users who land here via a direct nav we still clear and
# redirect.
@router.get("/auth/logout", response_class=HTMLResponse)
async def logout_get(request: Request) -> RedirectResponse:
    return await logout_submit(request)
