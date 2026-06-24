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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth import (
    SESSION_COOKIE_NAME,
    authenticate,
    create_user,
    current_user_optional,
    current_user_required,
    issue_session,
    revoke_session,
)
from app.auth.email_check import check_email
from app.auth.magic import consume_magic_link, create_magic_link
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.auth.users import update_password
from app.logging_setup import get_logger
from app.smtp_delivery import send_email
from app.storage.db import get_connection
from app.web.rate_limit import allow as _rate_allow
from app.web.templates_engine import templates


def _wants_json(request: Request) -> bool:
    """True when the request is an inline fetch (wants JSON, not a page)."""
    return (
        request.headers.get("x-requested-with", "").lower() == "fetch"
        or "application/json" in request.headers.get("accept", "")
    )


def _magic_email_html(link: str) -> tuple[str, str]:
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
    return text, html

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


def _client_ip(request: Request) -> str:
    """Best-effort client IP, honouring the reverse proxy's X-Forwarded-For."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _cookie_secure(request: Request) -> bool:
    """Session-cookie Secure flag. True over real HTTPS ИЛИ за TLS-прокси,
    который ставит X-Forwarded-Proto=https (devtunnel / FastPanel)."""
    if request.headers.get("x-forwarded-proto", "").lower() == "https":
        return True
    return request.url.scheme == "https"


def _rate_limited(request: Request, bucket: str, max_events: int, window_seconds: int) -> bool:
    """True если IP превысил бюджет попыток для ``bucket`` (anti brute-force/спам)."""
    return not _rate_allow(f"{bucket}:{_client_ip(request)}", max_events, window_seconds)


def _too_many(request: Request) -> Response:
    """Дружелюбный 429 для throttled auth-эндпоинтов."""
    msg = "Слишком много попыток. Подожди минуту и попробуй снова."
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": msg}, status_code=429)
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<body style='font-family:system-ui,sans-serif;background:#0b0b0f;color:#eee;"
        "padding:3rem;text-align:center'><h2>⏳ " + msg + "</h2>"
        "<p><a href='/auth/login' style='color:#a78bfa'>Назад ко входу</a></p></body>",
        status_code=429,
    )


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
    if _rate_limited(request, "signup", 10, 3600):
        return _too_many(request)
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
    response = RedirectResponse(url=await _post_auth_dest(user["id"]), status_code=303)
    secure = _cookie_secure(request)
    _set_session_cookie(response, token, secure, 30 * 24 * 3600)
    return response


@router.post("/auth/login", response_class=HTMLResponse, response_model=None)
async def login_submit(
    request: Request,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    """Verify credentials, start a session. JSON for inline fetch, else page."""
    if _rate_limited(request, "login", 20, 3600):
        return _too_many(request)
    user = await authenticate(email, password)
    if user is None:
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": "Неверный email или пароль."}, status_code=401)
        return templates.TemplateResponse(
            request,
            "auth_login.html",
            {"title": "Войти", "active_nav": "", "error": "Неверный email или пароль.", "email": email},
            status_code=401,
        )
    ua = _trim_ua(request.headers.get("user-agent"))
    token, _expires_at = await issue_session(user["id"], user_agent=ua)
    dest = await _post_auth_dest(user["id"])
    secure = _cookie_secure(request)
    if _wants_json(request):
        response: Response = JSONResponse({"ok": True, "redirect": dest})
    else:
        response = RedirectResponse(url=dest, status_code=303)
    _set_session_cookie(response, token, secure, 30 * 24 * 3600)
    return response


# --- Magic-link (passwordless) --------------------------------------------


async def _user_id_for_email(email: str) -> int | None:
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT id FROM users WHERE email = ?", (email,))
        row = await cursor.fetchone()
    return int(row["id"]) if row else None


async def _post_auth_dest(user_id: int) -> str:
    """Owner → cabinet; everyone else → /pending (sandboxed by owner-gate)."""
    return "/now" if await is_owner(user_id) else "/pending"


@router.post("/auth/magic", response_class=HTMLResponse, response_model=None)
async def magic_request(
    request: Request,
    email: Annotated[str, Form()],
) -> Response:
    """Request a passwordless login link for an EXISTING account.

    Existing user → mint a magic link and email it. Unknown email →
    generic response, NO account creation (anti-abuse on a public domain:
    blind account creation + spam-blast). New accounts go through the
    explicit /auth/signup form. Rate-limited per IP.

    The link is NEVER shown in the HTTP response: otherwise anyone could
    type someone else's email and log in as them. When SMTP isn't set up,
    the link is logged server-side so the owner can still test.
    """
    if _rate_limited(request, "magic", 5, 3600):
        return _too_many(request)
    chk = check_email(email)
    json_mode = _wants_json(request)
    # Невалидный формат ИЛИ распознанная опечатка домена (gmail.ru→gmail.com)
    # → не отправляем вслепую, показываем подсказку (работает и без JS).
    if not chk["valid"] or chk["suggestion"]:
        if json_mode:
            return JSONResponse({
                "ok": False,
                "error": "Похоже, email с ошибкой.",
                "suggestion": chk["suggestion"],
            }, status_code=400)
        return templates.TemplateResponse(
            request, "auth_magic_sent.html",
            {"title": "Проверьте email", "mode": "error", "email": email, "suggestion": chk["suggestion"]},
            status_code=400,
        )
    addr = chk["email"]
    uid = await _user_id_for_email(addr)
    registered_now = False
    if uid is None:
        # Публичный домен: НЕ создаём аккаунт вслепую по magic-ссылке
        # (анти-абуз: рассылка спама + бесконтрольное создание юзеров).
        # Ответ одинаковый, чтобы не палить, существует ли аккаунт.
        # Новые аккаунты — только через явный /auth/signup.
        if json_mode:
            return JSONResponse({
                "ok": True, "delivered": True, "registered": False,
                "message": f"Если аккаунт {addr} существует — ссылка для входа отправлена.",
            })
        return templates.TemplateResponse(
            request, "auth_magic_sent.html",
            {"title": "Проверьте почту", "mode": "login", "email": addr,
             "delivered": True, "registered": False},
        )

    token = await create_magic_link(addr)
    link = str(request.base_url).rstrip("/") + f"/auth/magic/{token}"
    text, html = _magic_email_html(link)
    result = await send_email(addr, "Вход в Persona", text, html)
    delivered = result.get("status") == "sent"
    if not delivered:
        log.warning("magic.not_emailed", status=result.get("status"))
    if json_mode:
        msg = (
            f"Аккаунт создан 🎉 Ссылка для входа отправлена на {addr}."
            if registered_now else f"Ссылка для входа отправлена на {addr}."
        ) if delivered else (
            "Аккаунт создан. " if registered_now else ""
        ) + "Письмо не ушло (SMTP не настроен) — пока войди по паролю или настрой почту."
        return JSONResponse({"ok": True, "delivered": delivered, "registered": registered_now, "message": msg})
    return templates.TemplateResponse(
        request, "auth_magic_sent.html",
        {"title": "Проверьте почту", "mode": "login", "email": addr,
         "delivered": delivered, "registered": registered_now},
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
    # ?next=/safe/path (например, сброс пароля). Только внутренние пути.
    nxt = request.query_params.get("next", "")
    dest = nxt if nxt.startswith("/") and not nxt.startswith("//") else await _post_auth_dest(uid)
    response = RedirectResponse(url=dest, status_code=303)
    secure = _cookie_secure(request)
    _set_session_cookie(response, token2, secure, 30 * 24 * 3600)
    return response


@router.post("/auth/forgot", response_class=HTMLResponse, response_model=None)
async def forgot_password(
    request: Request,
    email: Annotated[str, Form()],
) -> Response:
    """Сброс пароля через письмо: шлём magic-link на страницу установки пароля.
    Ответ одинаковый независимо от наличия аккаунта (не палим, кто зареган)."""
    if _rate_limited(request, "forgot", 5, 3600):
        return _too_many(request)
    chk = check_email(email)
    json_mode = _wants_json(request)
    generic = "Если такой аккаунт есть — на почту отправлена ссылка для смены пароля."
    if chk["valid"] and not chk["suggestion"]:
        uid = await _user_id_for_email(chk["email"])
        if uid is not None:
            token = await create_magic_link(chk["email"])
            link = str(request.base_url).rstrip("/") + f"/auth/magic/{token}?next=/auth/set-password"
            text, html = _magic_email_html(link)
            result = await send_email(chk["email"], "Смена пароля Persona", text, html)
            if result.get("status") != "sent":
                log.warning("forgot.not_emailed", status=result.get("status"))
    if json_mode:
        return JSONResponse({"ok": True, "message": generic})
    return templates.TemplateResponse(
        request, "auth_magic_sent.html",
        {"title": "Проверьте почту", "mode": "login", "email": chk.get("email", email), "delivered": True},
    )


@router.get("/auth/set-password", response_class=HTMLResponse, response_model=None)
async def set_password_page(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> Response:
    if session is None:
        return RedirectResponse(url="/landing", status_code=303)
    return templates.TemplateResponse(
        request, "auth_set_password.html",
        {"title": "Новый пароль", "email": session.get("email"), "error": None},
    )


@router.post("/auth/set-password", response_class=HTMLResponse, response_model=None)
async def set_password_submit(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    password: Annotated[str, Form()],
) -> Response:
    try:
        await update_password(int(session["user_id"]), password)
    except ValueError as exc:
        msg = "Пароль должен быть минимум 8 символов." if "8" in str(exc) else str(exc)
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": msg}, status_code=400)
        return templates.TemplateResponse(
            request, "auth_set_password.html",
            {"title": "Новый пароль", "email": session.get("email"), "error": msg}, status_code=400,
        )
    dest = await _post_auth_dest(int(session["user_id"]))
    if _wants_json(request):
        return JSONResponse({"ok": True, "redirect": dest})
    return RedirectResponse(url=dest, status_code=303)


@router.get("/pending", response_class=HTMLResponse, response_model=None)
async def pending_page(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> Response:
    """Holding page for non-owner accounts (sandboxed by the owner-gate)."""
    if session is None:
        return RedirectResponse(url="/landing", status_code=303)
    if await is_owner(session.get("user_id")):
        return RedirectResponse(url="/now", status_code=303)
    return templates.TemplateResponse(
        request,
        "auth_pending.html",
        {"title": "Аккаунт ожидает доступа", "email": session.get("email")},
    )


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
