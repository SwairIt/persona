"""Публичная форма поддержки — ``/support``.

Открыта ВСЕМ, включая анонимов из интернета (путь добавлен в
``_PUBLIC_PREFIXES`` в ``app/web/middleware/auth_gate.py``). Это осознанно:
человек, который не может войти, — как раз тот, кому нужнее всего написать.

Что здесь есть и почему
-----------------------
* **Работает без JS.** Обычный ``<form method="post">``, ошибки приезжают
  перерисованной страницей, успех — 303-редиректом (POST/Redirect/GET, чтобы
  F5 не отправлял обращение второй раз). Ни одна проверка не живёт в браузере.
* **Анти-абуз** (это публичная ручка записи на маленьком сервере):
  ловушка-honeypot, подписанное время выдачи формы, потолки длины и
  ЧЕТЫРЕ окна rate-limit — см. блок «Лимиты» ниже.
* **Отказ честный.** Никаких «спасибо» боту и никакого молчаливого
  выбрасывания: см. рассуждение в :mod:`app.support.service`.

Весь SQL — в :mod:`app.support.repository` (архитектурный гейт запрещает
роутам прямой доступ к БД).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.background import BackgroundTask

from app import __version__ as APP_VERSION
from app.auth import current_user_optional
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.support import notify, repository, service
from app.web import rate_limit
from app.web.templates_engine import templates

router = APIRouter(tags=["support"])
log = get_logger("persona.support.form")

# ── Лимиты ──────────────────────────────────────────────────────────────────
#
# Четыре окна, потому что они отвечают на разные вопросы:
#   burst  — «один и тот же человек долбит кнопку/скриптом прямо сейчас»;
#   ip_day — «с одного адреса за сутки пришло больше, чем бывает у людей»;
#   user   — то же для залогиненного (IP у него меняется — телефон, вайфай);
#   global — потолок ВСЕГО инстанса: против распределённого флуда, при
#            котором каждый отдельный адрес формально в рамках.
#
# ЧЕСТНЫЕ ГРАНИЦЫ (это важно знать до того, как на них понадеются):
# ``app.web.rate_limit`` — счётчик В ПАМЯТИ ПРОЦЕССА. Под ``uvicorn
# --workers 3`` реальный потолок втрое выше указанного, а рестарт обнуляет
# окна. Точный глобальный лимит потребовал бы Redis; для формы обратной связи
# это не окупается, а порядок величины («не тысяча в час») выдерживается.
_BURST_MAX, _BURST_WINDOW = 2, 120
_IP_MAX, _IP_WINDOW = 6, 24 * 3600
_USER_MAX, _USER_WINDOW = 10, 24 * 3600
_GLOBAL_MAX, _GLOBAL_WINDOW = 60, 3600


def _client_ip(request: Request) -> str:
    """IP клиента. ``X-Forwarded-For`` — ТОЛЬКО от доверенного прокси.

    Иначе любой бот подставил бы себе новый адрес в заголовке и обошёл все
    четыре окна разом. Та же логика, что в ``app/web/routes/auth.py``; там она
    приватная и завязана на биллинг-вебхук, поэтому здесь она повторена, а не
    импортирована через подчёркивание.
    """
    from app.auth import proxies  # noqa: PLC0415 — держим импорт вне старта

    peer = request.client.host if request.client else ""
    xff = request.headers.get("x-forwarded-for", "")
    if proxies.is_trusted_peer_sync(peer, proxies.trusted_networks_sync()):
        if xff:
            return xff.split(",")[0].strip()
        return peer
    if xff:
        proxies.note_untrusted_xff(peer, request.url.path)
    return peer


def _referer_path(request: Request) -> str:
    """Путь страницы, с которой человек пришёл на форму. Пустая строка, если нет.

    Берём ТОЛЬКО путь: в ``Referer`` целиком приезжают чужие домены и
    query-string с токенами, а нужен один вопрос — «на какой странице
    сломалось». Отрезание делает :func:`app.support.service.source_path`.
    """
    from urllib.parse import urlsplit  # noqa: PLC0415

    raw = request.headers.get("referer", "")
    if not raw:
        return ""
    try:
        return service.source_path(urlsplit(raw).path)
    except Exception:  # noqa: BLE001 — кривой Referer не ломает страницу
        return ""


async def _page(
    request: Request,
    session: SessionRecord | None,
    *,
    fields: dict[str, str] | None = None,
    error: str | None = None,
    sent_id: int | None = None,
    from_page: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Отрисовать форму. Одна функция на все состояния (пусто / ошибка / успех)."""
    account_email = ""
    if session is not None:
        account_email = await repository.user_email(int(session["user_id"]))
    return templates.TemplateResponse(
        request,
        "support.html",
        {
            "title": "Написать в поддержку",
            "session": session,
            "form_ts": await repository.sign_form_ts(),
            "from_page": from_page if from_page is not None else _referer_path(request),
            "fields": fields or {"subject": "", "body": "", "email": ""},
            "error": error,
            "sent_id": sent_id,
            "logged_in": session is not None,
            "account_email": account_email,
            "subject_max": service.SUBJECT_MAX,
            "body_max": service.BODY_MAX,
        },
        status_code=status_code,
    )


@router.get("/support", response_class=HTMLResponse)
async def support_form(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)] = None,
    sent: int = 0,
) -> HTMLResponse:
    return await _page(request, session, sent_id=int(sent) or None)


@router.post("/support", response_model=None)
async def support_submit(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)] = None,
) -> Any:
    """Принять обращение.

    Порядок шагов выбран так, чтобы дешёвые отказы случались раньше дорогих
    действий, а честный человек не расплачивался квотой за опечатку:

    1. ``burst``-окно по IP — единственная проверка ДО разбора формы;
    2. валидация (ловушка, подписанное время, адрес, длины) — без ввода-вывода;
    3. остальные окна лимита — тратятся только на ПРОШЕДШУЮ проверку отправку;
    4. запись в БД — обязательная часть;
    5. письмо владельцу — best-effort, исход пишется на обращение.
    """
    ip = _client_ip(request)
    if not rate_limit.allow(f"support:burst:{ip}", _BURST_MAX, _BURST_WINDOW):
        log.info("support.rate_limited", bucket="burst")
        return await _page(
            request,
            session,
            error=(
                "Слишком часто. Подожди пару минут — обращение никуда не денется."
            ),
            status_code=429,
        )

    form = await request.form()
    src_page = service.source_path(str(form.get("from") or ""))
    seconds = await repository.verify_form_ts(str(form.get("ts") or ""))
    fields, rejection = service.validate(
        subject=form.get("subject"),
        body=form.get("body"),
        email=form.get("email"),
        honeypot=form.get("website"),
        seconds_on_form=seconds,
        logged_in=session is not None,
    )
    if rejection is not None:
        log.info("support.rejected", reason=rejection.code)
        return await _page(
            request,
            session,
            fields=fields,
            error=rejection.message,
            from_page=src_page,
            status_code=400,
        )

    uid = int(session["user_id"]) if session is not None else None
    if uid is not None and not rate_limit.allow(
        f"support:user:{uid}", _USER_MAX, _USER_WINDOW
    ):
        log.info("support.rate_limited", bucket="user")
        return await _page(
            request,
            session,
            fields=fields,
            error="За сутки уже отправлено много обращений. Ответ придёт на прежние.",
            from_page=src_page,
            status_code=429,
        )
    if not rate_limit.allow(f"support:ip:{ip}", _IP_MAX, _IP_WINDOW):
        log.info("support.rate_limited", bucket="ip")
        return await _page(
            request,
            session,
            fields=fields,
            error="За сутки уже отправлено много обращений. Ответ придёт на прежние.",
            from_page=src_page,
            status_code=429,
        )
    if not rate_limit.allow("support:global", _GLOBAL_MAX, _GLOBAL_WINDOW):
        log.warning("support.rate_limited", bucket="global")
        return await _page(
            request,
            session,
            fields=fields,
            error=(
                "Сейчас поддержка перегружена обращениями. Попробуй через час — "
                "или напиши владельцу напрямую."
            ),
            from_page=src_page,
            status_code=429,
        )

    # Залогиненному адрес берём ИЗ АККАУНТА, а не из поля: поле у него
    # read-only и подставлено, а верить присланному значению нельзя — иначе
    # любой участник записал бы чужой адрес в качестве обратного.
    email = fields["email"]
    role = "anon"
    if uid is not None:
        email = await repository.user_email(uid) or email
        role = "owner" if bool(getattr(request.state, "is_owner", False)) else "member"

    ticket_id = await repository.create_ticket(
        user_id=uid,
        email=email,
        subject=fields["subject"],
        body=fields["body"],
        role=role,
        source_page=src_page,
        app_version=APP_VERSION,
        browser_class=service.browser_class(request.headers.get("user-agent")),
        ip_hash=await repository.hash_ip(ip),
    )
    log.info("support.ticket_created", ticket_id=ticket_id, role=role)

    # Письмо владельцу — СТРОГО после записи, строго best-effort и строго ПОСЛЕ
    # ответа посетителю (BackgroundTask: Starlette отдаёт редирект, а потом
    # выполняет задачу).
    #
    # Почему не inline. ИЗМЕРЕНО на этом сервере: исходящий 587 закрыт, и
    # ``aiosmtplib`` возвращает отказ только через ~16 секунд. Inline это
    # означало бы, что «Отправить» у постороннего человека висит полминуты и
    # выглядит как сломанный сайт — при том, что обращение УЖЕ сохранено и
    # исход письма ему всё равно не показывают. Дополнительный потолок
    # ожидания стоит внутри ``notify`` (``_SEND_TIMEOUT``), чтобы и фоновая
    # задача не сидела на сокете бесконечно.
    ticket = {
        "id": ticket_id,
        "subject": fields["subject"],
        "body": fields["body"],
        "email": email,
        "role": role,
        "source_page": src_page,
        "app_version": APP_VERSION,
        "browser_class": service.browser_class(request.headers.get("user-agent")),
    }
    return RedirectResponse(
        url=f"/support?sent={ticket_id}",
        status_code=303,
        background=BackgroundTask(_notify_owner_later, ticket_id, ticket),
    )


async def _notify_owner_later(ticket_id: int, ticket: dict[str, Any]) -> None:
    """Фоновая попытка уведомить владельца. НИЧЕГО не бросает наружу.

    Исключение здесь уже не может испортить посетителю отправку (ответ ушёл),
    но незамеченное — испортит ЛОГ и оставит обращение навсегда в статусе
    ``pending``, из-за чего владелец решит, что уведомление ещё едет.
    Поэтому оба шага обёрнуты, а исход пишется в любом случае.
    """
    try:
        outcome = await notify.notify_owner(ticket, await repository.owner_email())
    except Exception as exc:  # noqa: BLE001 — фоновая почта не роняет воркер
        log.warning("support.notify_failed", error=str(exc), ticket_id=ticket_id)
        outcome = "error:непредвиденный сбой уведомления"
    try:
        await repository.mark_owner_notified(ticket_id, outcome)
    except Exception as exc:  # noqa: BLE001 — и запись исхода тоже
        log.warning("support.notify_mark_failed", error=str(exc))
