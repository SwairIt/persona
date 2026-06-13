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
from app.auth.email_check import check_email
from app.auth.magic import consume_magic_link, create_magic_link
from app.auth.sessions import SessionRecord
from app.auth.waitlist import add_to_waitlist
from app.logging_setup import get_logger
from app.smtp_delivery import send_email
from app.storage.db import get_connection
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


# --- Magic-link (passwordless) --------------------------------------------


async def _user_id_for_email(email: str) -> int | None:
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT id FROM users WHERE email = ?", (email,))
        row = await cursor.fetchone()
    return int(row["id"]) if row else None


@router.post("/auth/magic", response_class=HTMLResponse, response_model=None)
async def magic_request(
    request: Request,
    email: Annotated[str, Form()],
) -> Response:
    """Request a passwordless login link.

    Existing user → mint a magic link and email it. Unknown email → add to
    waitlist (open registration is gated until per-user data isolation).
    The link is NEVER shown in the HTTP response: otherwise anyone could
    type someone else's email and log in as them. When SMTP isn't set up,
    the link is logged server-side so the owner can still test.
    """
    chk = check_email(email)
    # Невалидный формат ИЛИ распознанная опечатка домена (gmail.ru→gmail.com)
    # → не отправляем вслепую, показываем подсказку (работает и без JS).
    if not chk["valid"] or chk["suggestion"]:
        return templates.TemplateResponse(
            request,
            "auth_magic_sent.html",
            {
                "title": "Проверьте email",
                "mode": "error",
                "email": email,
                "suggestion": chk["suggestion"],
            },
            status_code=400,
        )
    addr = chk["email"]
    uid = await _user_id_for_email(addr)
    if uid is not None:
        token = await create_magic_link(addr)
        link = str(request.base_url).rstrip("/") + f"/auth/magic/{token}"
        text = (
            "Чтобы войти в Persona, открой ссылку:\n\n"
            f"{link}\n\n"
            "Ссылка действует 30 минут и срабатывает один раз. "
            "Если ты не запрашивал вход — просто проигнорируй письмо."
        )
        html = (
            "<p>Чтобы войти в Persona, нажми кнопку:</p>"
            f'<p><a href="{link}" '
            'style="display:inline-block;padding:12px 22px;border-radius:10px;'
            'background:#7c3aed;color:#fff;text-decoration:none">Войти в Persona</a></p>'
            "<p>Ссылка действует 30 минут и срабатывает один раз.</p>"
        )
        result = await send_email(addr, "Вход в Persona", text, html)
        if result.get("status") != "sent":
            # SMTP не настроен/ошибка — НЕ светим ссылку в ответе, только в лог.
            log.warning("magic.not_emailed", status=result.get("status"), link=link)
        return templates.TemplateResponse(
            request,
            "auth_magic_sent.html",
            {
                "title": "Проверьте почту",
                "mode": "login",
                "email": addr,
                "delivered": result.get("status") == "sent",
            },
        )
    await add_to_waitlist(addr)
    return templates.TemplateResponse(
        request,
        "auth_magic_sent.html",
        {"title": "Вы в списке", "mode": "waitlist", "email": addr},
    )


@router.get("/auth/magic/{token}", response_class=HTMLResponse, response_model=None)
async def magic_consume(request: Request, token: str) -> Response:
    """Consume a magic link → issue a session → redirect to the cabinet."""
    addr = await consume_magic_link(token)
    if addr is None:
        return templates.TemplateResponse(
            request,
            "auth_magic_sent.html",
            {"title": "Ссылка недействительна", "mode": "invalid", "email": ""},
            status_code=400,
        )
    uid = await _user_id_for_email(addr)
    if uid is None:
        return templates.TemplateResponse(
            request,
            "auth_magic_sent.html",
            {"title": "Аккаунт не найден", "mode": "invalid", "email": addr},
            status_code=400,
        )
    ua = _trim_ua(request.headers.get("user-agent"))
    token2, _expires_at = await issue_session(uid, user_agent=ua)
    response = RedirectResponse(url="/now", status_code=303)
    secure = request.url.scheme == "https"
    _set_session_cookie(response, token2, secure, 30 * 24 * 3600)
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
