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

import html as html_lib
import secrets
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth import (
    SESSION_COOKIE_NAME,
    authenticate,
    create_user,
    current_user_optional,
    current_user_required,
    revoke_all_for_user,
    revoke_session,
    rotate_session,
    verify_session,
)
from app.auth import lockout as _lockout
from app.auth import proxies as _proxies
from app.auth.email_check import check_email
from app.auth.exclusive import owner_exclusive_enabled
from app.auth.magic import consume_magic_link, create_magic_link
from app.auth.owner import is_owner, is_primary_owner
from app.auth.account_state import AccountInactiveError
from app.auth.sessions import SessionRecord
from app.auth.users import is_account_active, update_password
from app.auth.verification import mark_verified
from app.logging_setup import get_logger
from app.mail_branding import branded_email_html
from app.smtp_delivery import send_email
from app.storage.db import get_connection
from app.storage.repository import get_kv
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
    html = branded_email_html(
        preheader="Ссылка для входа в Persona — 30 минут, один раз.",
        heading="Вход в Persona",
        lead="Нажми кнопку — войдёшь без пароля. Ссылка действует 30 минут и срабатывает один раз.",
        button_label="Войти в Persona",
        button_url=link,
        footer="Если ты не запрашивал вход — просто проигнорируй это письмо.",
    )
    return text, html


def _gen_password() -> str:
    """Случайный читаемый пароль для авто-регистрации (≥8 символов, urlsafe)."""
    return secrets.token_urlsafe(9)  # ~12 символов


def _welcome_email_html(addr: str, password: str, login_url: str, setpw_url: str) -> tuple[str, str]:
    text = (
        "Добро пожаловать в Persona!\n\n"
        f"Аккаунт создан: {addr}\n"
        f"Пароль: {password}\n\n"
        f"Войти: {login_url}\n"
        f"Сменить пароль (рекомендуем): {setpw_url}\n\n"
        "Если ты не регистрировался — просто проигнорируй это письмо."
    )
    safe_addr = html_lib.escape(addr)
    safe_pw = html_lib.escape(password)
    pwbox = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:2px 0 14px;">'
        '<tr><td style="background:#130a2e;border:1px solid rgba(147,130,255,.25);border-radius:12px;padding:14px 18px;">'
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.4;color:#9a90c0;margin-bottom:5px;">Твой пароль</div>'
        f"<div style=\"font-family:'Courier New',monospace;font-size:21px;font-weight:700;color:#e9e3ff;letter-spacing:1px;\">{safe_pw}</div>"
        "</td></tr></table>"
        '<p style="margin:0 0 6px;font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.6;color:#9a90c0;">'
        f'Рекомендуем сразу <a href="{setpw_url}" style="color:#c4b5fd;">сменить пароль</a> в настройках.</p>'
    )
    html = branded_email_html(
        preheader="Твой аккаунт в Persona создан — пароль внутри.",
        heading="Добро пожаловать 🎉",
        lead=f'Аккаунт <b style="color:#e9e3ff;">{safe_addr}</b> готов. Вот пароль для входа — '
        "войди и при желании поменяй его.",
        button_label="Войти в Persona",
        button_url=login_url,
        extra_html=pwbox,
        footer="Если ты не регистрировался — просто проигнорируй это письмо.",
    )
    return text, html


router = APIRouter(tags=["auth"])
log = get_logger("persona.auth.routes")


# Статусы ``send_email``, которые означают «почта на этом сервере не настроена»
# (в отличие от ``error`` — попытались отправить, но релей отказал).
_MAIL_UNCONFIGURED = frozenset({"disabled", "misconfigured", "missing_dep"})


async def _send_mail_safe(
    to_addr: str, subject: str, text: str, html: str | None, *, flow: str
) -> str:
    """Отправить письмо, НИКОГДА не роняя запрос. Возвращает статус-строку.

    ``send_email`` уже ловит сетевые/SMTP-ошибки, но чтение настроек (kv/БД) и
    сборка сообщения — нет. Регистрация и вход НЕ должны зависеть от почты:
    любой сбой здесь = warning в лог + честный статус наверх, никогда 500.

    ``flow`` — имя сценария для логов (``register`` / ``magic`` / ``forgot``).
    Именно ``flow``, а не ``event``: ``event`` занят самим structlog.
    """
    try:
        result = await send_email(to_addr, subject, text, html)
    except Exception as exc:  # noqa: BLE001 — почта не может уронить auth-флоу
        log.warning("mail.send_crashed", flow=flow, error=str(exc))
        return "error"
    status = str(result.get("status") or "error")
    if status != "sent":
        log.warning("mail.not_delivered", flow=flow, status=status)
    return status


# Cap on the User-Agent we persist into auth_session. The longest UAs
# we've seen are around 200 chars; anything beyond is junk.
_MAX_UA_LEN = 250

# Доверенные reverse-proxy (кому верим X-Forwarded-For) переехали в
# app/auth/proxies.py: теперь это env ``PERSONA_TRUSTED_PROXIES`` / kv
# ``trusted_proxies`` с ТЕМИ ЖЕ значениями по умолчанию ({127.0.0.1,
# 192.168.33.3}), поддержкой CIDR и громким одноразовым warning'ом, когда XFF
# приходит от недоверенного пира. Инструкция по проверке против живого прокси —
# в докстринге того модуля.
#
# Имя оставлено для обратной совместимости (тесты/скрипты могли на него
# ссылаться); фактический источник истины — ``proxies.trusted_networks_sync()``.
_TRUSTED_PROXIES = set(_proxies.DEFAULT_TRUSTED_PROXIES)


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
    """Best-effort client IP. Доверяем X-Forwarded-For ТОЛЬКО когда прямой
    peer — известный reverse-proxy (см. :mod:`app.auth.proxies`). Иначе берём
    request.client.host: иначе кто угодно подделал бы XFF и обошёл rate-limit.

    Функция СИНХРОННАЯ намеренно: её импортирует биллинг-вебхук
    (``app/web/routes/billing.py``) для IP-фильтра ЮKassa. Конфиг читается из
    прогретого кэша ``proxies.trusted_networks_sync()`` — без похода в БД на
    горячем пути. Если XFF пришёл от НЕдоверенного пира, один раз на процесс
    пишем громкий warning (``auth.proxy.untrusted_xff``) и заголовок
    игнорируем — fail-safe: лучше ограничить по IP прокси, чем поверить
    подделке.
    """
    peer = request.client.host if request.client else "unknown"
    xff = request.headers.get("x-forwarded-for", "")
    if _proxies.is_trusted_peer_sync(peer, _proxies.trusted_networks_sync()):
        if xff:
            return xff.split(",")[0].strip()
        return peer
    if xff:
        _proxies.note_untrusted_xff(peer, request.url.path)
    return peer


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


def _registration_disabled(request: Request) -> Response:
    """Return a non-enumerating owner-only enrollment refusal."""
    message = "Регистрация отключена: эта Persona доступна только владельцу."
    if _wants_json(request):
        return JSONResponse({"ok": False, "error": message}, status_code=403)
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8>"
        "<body style='font-family:system-ui,sans-serif;background:#0b0b0f;color:#eee;"
        "padding:3rem;text-align:center'><h2>Доступ только владельцу</h2>"
        f"<p>{html_lib.escape(message)}</p>"
        "<p><a href='/auth/login' style='color:#a78bfa'>Войти</a></p></body>",
        status_code=403,
    )


# Перевод стабильных английских ключей валидации в RU-копию. Ключи задаются в
# app/auth/users.py и app/auth/password_policy.py; неизвестный ключ показываем
# как есть — так новая проверка не теряет текст, пока её не перевели.
_RU_ERRORS: dict[str, str] = {
    "email already registered": "Этот email уже зарегистрирован.",
    "invalid email": "Неверный формат email.",
    "password must be at least 8 characters": "Пароль должен быть минимум 8 символов.",
    "password must be at most 1024 characters": "Пароль слишком длинный.",
    "password is too common": (
        "Такой пароль есть в списке самых частых — его подбирают за секунды. "
        "Придумай другой."
    ),
    "password is too simple": (
        "Слишком простой пароль (подряд идущие символы или один и тот же). "
        "Придумай другой."
    ),
    "password must not contain your email": (
        "Пароль не должен содержать твой email — это первое, что перебирают."
    ),
}


def _ru_error(raw: str) -> str:
    """Русский текст ошибки валидации по стабильному английскому ключу."""
    return _RU_ERRORS.get(raw, raw)


# Копия для аккаунтов с ``users.status != 'active'``. Показывается ТОЛЬКО после
# верного пароля, поэтому перечислением заблокированные аккаунты не находятся.
_INACTIVE_MESSAGES: dict[str, str] = {
    "suspended": (
        "Этот аккаунт заблокирован. Если считаешь, что это ошибка — "
        "напиши владельцу Persona."
    ),
    "pending": (
        "Этот аккаунт ещё не активирован. Дождись подтверждения от владельца."
    ),
}


async def _exclusive_allows(user_id: int | None) -> bool:
    """Allow everyone in normal mode, only the primary owner in exclusive mode."""
    if not await owner_exclusive_enabled():
        return True
    return await is_primary_owner(user_id)


# --- GET pages -------------------------------------------------------------


@router.get("/auth/signup", response_class=HTMLResponse, response_model=None)
async def signup_page(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> HTMLResponse | RedirectResponse:
    """Render the signup form, or bounce to /now when already signed in."""
    if await owner_exclusive_enabled():
        return RedirectResponse(url="/auth/login", status_code=303)
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
    exclusive = await owner_exclusive_enabled()
    if session is not None:
        if exclusive and not await is_primary_owner(session.get("user_id")):
            await revoke_session(session["token"])
            response = RedirectResponse(url="/auth/login", status_code=303)
            response.delete_cookie(SESSION_COOKIE_NAME, path="/")
            return response
        return RedirectResponse(url="/now", status_code=303)
    return templates.TemplateResponse(
        request,
        "auth_login.html",
        {
            "title": "Войти",
            "active_nav": "",
            "error": None,
            "email": "",
            "registration_enabled": not exclusive,
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
    if await owner_exclusive_enabled():
        return _registration_disabled(request)
    if _rate_limited(request, "signup", 10, 3600):
        return _too_many(request)
    try:
        user = await create_user(email, password, display_name)
    except ValueError as exc:
        raw = str(exc)
        # Translate the few known error keys; anything else is shown
        # verbatim (catches future validator additions without losing info).
        ru = _ru_error(raw)
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
    # Ротация, а не просто выдача: если в браузере уже лежала чужая/подсунутая
    # кука сессии, она умирает здесь, а не живёт параллельно новой.
    token, _expires_at = await rotate_session(
        request.cookies.get(SESSION_COOKIE_NAME), user["id"], user_agent=ua
    )
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
    """Verify credentials, start a session. JSON for inline fetch, else page.

    Two independent throttles guard this handler:

    * **per-IP** (existing, 20/hour) — stops one box hammering the endpoint;
    * **per-ACCOUNT** (:mod:`app.auth.lockout`, exponential backoff after 5
      failures) — an attacker rotating through a proxy pool resets the IP
      counter for free, but not the counter on the address he is guessing.

    The account check runs *before* ``authenticate`` so a locked account costs
    the server zero PBKDF2 work (600 000 iterations ≈ 250 ms of CPU each — an
    unthrottled login endpoint is a CPU-exhaustion DoS as much as a
    credential-stuffing surface).
    """
    if _rate_limited(request, "login", 20, 3600):
        return _too_many(request)
    if _lockout.locked_for(email) > 0:
        # Same response as the per-IP limit: no account enumeration, because
        # the counter is keyed on whatever string was submitted, existing or not.
        return _too_many(request)
    try:
        user = await authenticate(email, password)
    except AccountInactiveError as exc:
        # Пароль ВЕРНЫЙ, но аккаунт заблокирован/не допущен. Показать это можно
        # честно: чтобы сюда попасть, надо уже знать пароль — перечислением
        # заблокированные аккаунты не вычисляются (неверный пароль всегда даёт
        # обычное «неверный email или пароль»).
        _lockout.clear(email)
        log.warning("auth.login.inactive_account", status=exc.status)
        message = _INACTIVE_MESSAGES.get(exc.status, _INACTIVE_MESSAGES["suspended"])
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": message}, status_code=403)
        return templates.TemplateResponse(
            request,
            "auth_login.html",
            {"title": "Войти", "active_nav": "", "error": message, "email": email},
            status_code=403,
        )
    if user is None or not await _exclusive_allows(user["id"]):
        _lockout.record_failure(email)
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": "Неверный email или пароль."}, status_code=401)
        return templates.TemplateResponse(
            request,
            "auth_login.html",
            {"title": "Войти", "active_nav": "", "error": "Неверный email или пароль.", "email": email},
            status_code=401,
        )
    _lockout.clear(email)
    ua = _trim_ua(request.headers.get("user-agent"))
    # Session fixation: if a session cookie is already present, replace it
    # rather than adding a second live token beside it.
    token, _expires_at = await rotate_session(
        request.cookies.get(SESSION_COOKIE_NAME), user["id"], user_agent=ua
    )
    dest = await _post_auth_dest(user["id"])
    secure = _cookie_secure(request)
    if _wants_json(request):
        response: Response = JSONResponse({"ok": True, "redirect": dest})
    else:
        response = RedirectResponse(url=dest, status_code=303)
    _set_session_cookie(response, token, secure, 30 * 24 * 3600)
    return response


@router.post("/auth/register", response_class=HTMLResponse, response_model=None)
async def register_submit(
    request: Request,
    email: Annotated[str, Form()],
) -> Response:
    """Авто-регистрация по одному email: создаём аккаунт со случайным паролем,
    ПОКАЗЫВАЕМ пароль на экране и сразу логиним. Существующий email → ссылка для
    входа (аккаунт НЕ пересоздаём, пароль не палим). Опечатки доменов (gmail.ru
    и т.п.) блокируются через ``check_email``. Rate-limit как у остальных
    auth-роутов.

    ВАЖНО (инвариант): экранный показ пароля БЕЗУСЛОВЕН. Раньше пароль уходил
    только письмом — а ``aiosmtplib`` не был установлен, поэтому письмо не
    уходило НИКОГДА, и зарегистрировавшийся получал аккаунт, в который не мог
    войти. Письмо теперь — дубль, а не единственный канал; если SMTP не
    настроен, страница честно об этом говорит, а не делает вид, что письмо ушло.
    Кто хочет свой пароль сразу — ``/auth/signup`` (форма email + пароль).
    """
    if await owner_exclusive_enabled():
        return _registration_disabled(request)
    if _rate_limited(request, "register", 5, 3600):
        return _too_many(request)
    chk = check_email(email)
    json_mode = _wants_json(request)
    if not chk["valid"] or chk["suggestion"]:
        err = "Похоже, в адресе опечатка." if chk["suggestion"] else "Неверный формат email."
        if json_mode:
            return JSONResponse(
                {"ok": False, "error": err, "suggestion": chk["suggestion"]}, status_code=400
            )
        return templates.TemplateResponse(
            request, "auth_magic_sent.html",
            {"title": "Проверьте email", "mode": "error", "email": email, "suggestion": chk["suggestion"]},
            status_code=400,
        )
    addr = chk["email"]
    base = str(request.base_url).rstrip("/")
    uid = await _user_id_for_email(addr)
    if uid is not None:
        # Аккаунт уже есть — не пересоздаём и не палим пароль: шлём ссылку для
        # входа. Заблокированному аккаунту ссылку НЕ выпускаем (иначе повторная
        # «регистрация» тем же адресом обходит suspension), но копию ответа не
        # меняем — снаружи заблокированный и обычный аккаунт неотличимы.
        active = await is_account_active(uid)
        if not active:
            log.warning("auth.register.existing_inactive")
        token = await create_magic_link(addr) if active else ""
        status = "disabled"
        if active:
            text, html = _magic_email_html(f"{base}/auth/magic/{token}")
            status = await _send_mail_safe(
                addr, "Вход в Persona", text, html, flow="register_existing"
            )
        delivered = status == "sent"
        if delivered:
            msg = f"У тебя уже есть аккаунт — отправили ссылку для входа на {addr}."
        elif status in _MAIL_UNCONFIGURED:
            msg = (
                "Аккаунт уже существует, но почта на этом сервере не настроена — "
                "письмо не уйдёт. Войди по паролю (кнопка «войти паролем»)."
            )
        else:
            msg = (
                "Аккаунт уже существует, но письмо отправить не удалось. "
                "Войди по паролю (кнопка «войти паролем»)."
            )
        if json_mode:
            return JSONResponse({"ok": True, "existing": True, "delivered": delivered, "message": msg})
        return templates.TemplateResponse(
            request, "auth_magic_sent.html",
            {"title": "Проверьте почту", "mode": "login", "email": addr, "delivered": delivered},
        )
    # Новый аккаунт со случайным паролем.
    password = _gen_password()
    try:
        user = await create_user(addr, password, None)
    except ValueError:
        # гонка (кто-то успел зарегать между проверкой и вставкой) — мягко на вход
        if json_mode:
            return JSONResponse(
                {"ok": False, "error": "Не удалось создать аккаунт, попробуй войти."}, status_code=400
            )
        return RedirectResponse(url="/auth/login", status_code=303)
    # Биллинг на этом MVP СПИТ: триал больше не заводим (иначе через 3 дня
    # участник видел «триал закончился, оформи Pro» над живыми кнопками оплаты,
    # хотя доступ бесплатный). ``billing_service.ensure_trial`` жив и доступен
    # владельцу — его просто никто не дёргает из регистрации.
    text, html = _welcome_email_html(addr, password, f"{base}/auth/login", f"{base}/auth/set-password")
    mail_status = await _send_mail_safe(
        addr, "Добро пожаловать в Persona — твой пароль", text, html, flow="register"
    )
    delivered = mail_status == "sent"
    if not delivered:
        log.warning(
            "register.not_emailed",
            status=mail_status,
            email_domain=addr.rpartition("@")[2],
        )
    ua = _trim_ua(request.headers.get("user-agent"))
    token, _expires_at = await rotate_session(
        request.cookies.get(SESSION_COOKIE_NAME), user["id"], user_agent=ua
    )
    dest = await _post_auth_dest(user["id"])
    secure = _cookie_secure(request)
    if delivered:
        msg = f"Аккаунт создан 🎉 Пароль ниже — он же продублирован письмом на {addr}. Сохрани его."
    elif mail_status in _MAIL_UNCONFIGURED:
        msg = (
            "Аккаунт создан 🎉 Почта на этом сервере не настроена, письма не будет — "
            "сохрани пароль ниже, другого способа его узнать нет."
        )
    else:
        msg = (
            "Аккаунт создан 🎉 Письмо с паролем отправить не удалось — "
            "сохрани пароль ниже, другого способа его узнать нет."
        )
    if json_mode:
        # ``password`` возвращаем НАМЕРЕННО: это единственный ответ на запрос,
        # который сам же создал аккаунт, и получатель уже залогинен этой же
        # куки. Ключ ``redirect`` убран специально — лендинг не должен увести
        # человека со страницы раньше, чем он увидит пароль (для перехода есть
        # ``next``).
        response: Response = JSONResponse(
            {
                "ok": True,
                "registered": True,
                "delivered": delivered,
                "password": password,
                "next": dest,
                "set_password_url": "/auth/set-password",
                "message": msg,
            }
        )
    else:
        response = templates.TemplateResponse(
            request,
            "auth_registered.html",
            {
                "title": "Аккаунт создан",
                "email": addr,
                "password": password,
                "delivered": delivered,
                "mail_unconfigured": mail_status in _MAIL_UNCONFIGURED,
                "next_url": dest,
                "message": msg,
            },
        )
    _set_session_cookie(response, token, secure, 30 * 24 * 3600)
    return response


# --- Magic-link (passwordless) --------------------------------------------


async def _user_id_for_email(email: str) -> int | None:
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT id FROM users WHERE email = ?", (email,))
        row = await cursor.fetchone()
    return int(row["id"]) if row else None


async def _post_auth_dest(user_id: int) -> str:
    """Владелец → приложение (/now). Любой участник (регистрация свободная,
    подписка НЕ спрашивается) → ИИ-ассистент: онбординг при первом входе,
    иначе сразу чат. Чат/память изолированы по user_id — чужого он не видит."""
    if await owner_exclusive_enabled() and not await is_primary_owner(user_id):
        return "/pending"
    if await is_owner(user_id):
        return "/now"
    async with get_connection() as conn:
        onboarded = await get_kv(conn, f"onboarded_{user_id}")
    return "/chat" if (onboarded or "").strip() == "1" else "/onboarding"


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
    if uid is not None and not await _exclusive_allows(uid):
        uid = None
    # Заблокированный/недопущенный аккаунт трактуем как несуществующий: ссылка
    # НЕ выдаётся (иначе magic-link — обход suspension), а ответ остаётся тем
    # же самым, что и для неизвестного адреса, — не палим статус.
    if uid is not None and not await is_account_active(uid):
        log.warning("auth.magic.refused_inactive")
        uid = None
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
    delivered = await _send_mail_safe(addr, "Вход в Persona", text, html, flow="magic") == "sent"
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
    if not await _exclusive_allows(uid):
        return templates.TemplateResponse(
            request,
            "auth_magic_sent.html",
            {"title": "Доступ запрещён", "mode": "invalid", "email": ""},
            status_code=403,
        )
    # Даже валидная одноразовая ссылка не должна открывать заблокированный
    # аккаунт: ссылку могли выпустить ДО блокировки.
    if not await is_account_active(uid):
        log.warning("auth.magic.consume_refused_inactive", user_id=uid)
        return templates.TemplateResponse(
            request,
            "auth_magic_sent.html",
            {"title": "Доступ запрещён", "mode": "invalid", "email": ""},
            status_code=403,
        )
    # Переход по ссылке, доставленной на адрес, — доказательство владения
    # почтой. Отдельного «подтвердите email» флоу не заводим: этого достаточно.
    # Неподтверждённые аккаунты не блокируются, но получают урезанные лимиты
    # (app/auth/verification.py + app/web/middleware/throttle.py).
    await mark_verified(uid)
    ua = _trim_ua(request.headers.get("user-agent"))
    token2, _expires_at = await rotate_session(
        request.cookies.get(SESSION_COOKIE_NAME), uid, user_agent=ua
    )
    # ?next=/safe/path (например, сброс пароля). Только внутренние пути:
    # начинается с одного «/», без «//» и «/\» (protocol-relative / backslash-
    # обходы), и без scheme/netloc — иначе это open-redirect наружу.
    nxt = request.query_params.get("next", "")
    parsed = urlparse(nxt)
    is_internal = (
        nxt.startswith("/")
        and not nxt.startswith("//")
        and not nxt.startswith("/\\")
        and parsed.scheme == ""
        and parsed.netloc == ""
    )
    dest = nxt if is_internal else await _post_auth_dest(uid)
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
        # Сброс пароля — тоже путь к сессии, поэтому заблокированному аккаунту
        # письмо не уходит. Ответ снаружи одинаковый в любом случае.
        if (
            uid is not None
            and await _exclusive_allows(uid)
            and await is_account_active(uid)
        ):
            token = await create_magic_link(chk["email"])
            link = str(request.base_url).rstrip("/") + f"/auth/magic/{token}?next=/auth/set-password"
            text, html = _magic_email_html(link)
            await _send_mail_safe(
                chk["email"], "Смена пароля Persona", text, html, flow="forgot"
            )
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
    if not await _exclusive_allows(session.get("user_id")):
        return HTMLResponse("Owner access required", status_code=403)
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
    if not await _exclusive_allows(session.get("user_id")):
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "error": "Доступ только владельцу."},
                status_code=403,
            )
        return HTMLResponse("Owner access required", status_code=403)
    try:
        await update_password(int(session["user_id"]), password)
    except ValueError as exc:
        msg = _ru_error(str(exc))
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": msg}, status_code=400)
        return templates.TemplateResponse(
            request, "auth_set_password.html",
            {"title": "Новый пароль", "email": session.get("email"), "error": msg}, status_code=400,
        )
    # Пароль сменён → гасим все непогашенные magic-ссылки этого email, чтобы
    # старая «забыл пароль»-ссылка из почты больше не открывала аккаунт.
    addr = session.get("email")
    if addr:
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE magic_link SET used_at = datetime('now') "
                "WHERE email = ? AND used_at IS NULL",
                (addr,),
            )
            await conn.commit()
    # Смена пароля — привилегированное событие. Все ОСТАЛЬНЫЕ сессии этого
    # аккаунта гасим (если пароль меняют после угона — чужая сессия обязана
    # умереть), а свою РОТИРУЕМ: новый токен вместо старого, чтобы утёкший
    # идентификатор не пережил смену пароля. Порядок важен: сначала гасим
    # чужие по старому токену (его ещё нельзя терять), потом ротируем свой.
    uid = int(session["user_id"])
    old_token = session.get("token") or request.cookies.get(SESSION_COOKIE_NAME)
    revoked = 0
    try:
        revoked = await revoke_all_for_user(uid, keep_token=old_token)
    except Exception as exc:  # noqa: BLE001 — смена пароля не должна падать
        log.warning("auth.password_change.revoke_failed", error=str(exc))
    ua = _trim_ua(request.headers.get("user-agent"))
    new_token, _exp = await rotate_session(old_token, uid, user_agent=ua)
    log.info("auth.password_change.sessions_revoked", user_id=uid, revoked=revoked)

    dest = await _post_auth_dest(uid)
    if _wants_json(request):
        response: Response = JSONResponse({"ok": True, "redirect": dest})
    else:
        response = RedirectResponse(url=dest, status_code=303)
    _set_session_cookie(response, new_token, _cookie_secure(request), 30 * 24 * 3600)
    return response


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
    if await owner_exclusive_enabled():
        await revoke_session(session["token"])
        response = RedirectResponse(url="/landing", status_code=303)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return response
    # не-владелец → бесплатная поверхность участника (ИИ-ассистент)
    return RedirectResponse(url="/chat", status_code=303)


@router.post("/auth/logout", response_model=None)
async def logout_submit(
    request: Request,
    scope: Annotated[str, Form()] = "",
) -> Response:
    """Revoke the session server-side and clear the cookie.

    Server-side revocation (``revoked_at`` stamped in ``auth_session``) is the
    part that matters: deleting the cookie alone leaves a token that is still
    accepted if it was ever copied out of the browser.

    ``scope=all`` — "выйти на всех устройствах": revokes every active session
    of this account, not just this browser. Implemented as a form field rather
    than a new route so the surface (and the route budget) does not grow;
    :func:`app.auth.sessions.revoke_all_for_user` does the work.
    """
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if token:
        if scope.strip().lower() == "all":
            session = await verify_session(token)
            if session is not None:
                await revoke_all_for_user(int(session["user_id"]))
        await revoke_session(token)
    response = RedirectResponse(url="/landing", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    # Производный CSRF-токен привязан к сессии — старая кука после выхода
    # бессмысленна и только мешает следующему входу.
    response.delete_cookie("persona_csrf", path="/")
    return response


# GET /auth/logout НЕ выходит — он показывает подтверждение с POST-формой.
#
# Почему: cookie ``persona_session`` — SameSite=Lax, а Lax НАМЕРЕННО шлёт куку
# при top-level GET-навигации. То есть любой ``<img src="/auth/logout">`` или
# ссылка с чужого сайта разлогинивала пользователя без единого клика — CSRF на
# выход (сам по себе это «всего лишь» отказ в обслуживании, но он же —
# первая половина login-CSRF: выбить человека из его аккаунта и подсунуть свой).
#
# Удалить GET-роут нельзя: на него ссылается хаб настроек
# (``settings_hub._MEMBER_CATEGORIES`` → ``("/auth/logout", "Выйти")``) обычной
# ссылкой, и 404 там сломал бы UI. Поэтому GET стал БЕЗОПАСНЫМ: он ничего не
# меняет, только рисует кнопку, которая делает POST.
@router.get("/auth/logout", response_class=HTMLResponse, response_model=None)
async def logout_get(request: Request) -> Response:
    """Safe GET: render a confirmation form instead of mutating state."""
    if request.cookies.get(SESSION_COOKIE_NAME) is None:
        return RedirectResponse(url="/landing", status_code=303)
    # Локальный импорт: держим middleware вне графа импорта роутера на старте.
    from app.web.middleware.csrf import csrf_input  # noqa: PLC0415

    csrf = str(csrf_input(request))
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><title>Выход</title>"
        "<body style='font-family:system-ui,sans-serif;background:#0b0b0f;color:#eee;"
        "padding:3rem;text-align:center'>"
        "<h2>Выйти из Persona?</h2>"
        "<form method='post' action='/auth/logout' style='margin-top:22px'>"
        f"{csrf}"
        "<button type='submit' style='background:#a78bfa;color:#0b0b0f;border:0;"
        "border-radius:10px;padding:12px 26px;font-size:15px;font-weight:600;"
        "cursor:pointer'>Выйти</button></form>"
        "<form method='post' action='/auth/logout' style='margin-top:12px'>"
        f"{csrf}"
        "<input type='hidden' name='scope' value='all'>"
        "<button type='submit' style='background:transparent;color:#9a90c0;border:0;"
        "font-size:13px;text-decoration:underline;cursor:pointer'>"
        "Выйти на всех устройствах</button></form>"
        "<p style='margin-top:22px'><a href='/chat' style='color:#a78bfa'>Отмена</a></p>"
        "</body>"
    )
