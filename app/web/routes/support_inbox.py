"""Ящик поддержки владельца — ``/settings/support``.

Здесь владелец читает обращения, меняет им статус и отвечает. Это ОСНОВНОЙ
канал: почта на инстансе не настроена (см. :mod:`app.support.notify`), и
сайт — единственное место, где обращение гарантированно видно.

Почему одна страница, а не «список + карточка»
----------------------------------------------
Мастер-деталь на одном пути (``?ticket=N``), а не отдельный
``/settings/support/{id}``. Это не экономия ради экономии: бюджет
зарегистрированных роутов (``REGISTERED_ROUTE_BUDGET``) заполнен, и каждая
новая ручка требует пересмотра. Разделение здесь ничего бы не дало — ящик
маленький, а «открыть обращение» это тот же список с раскрытым письмом.
Действия (прочитано / отвечено / закрыто / ответ) тоже сведены в ОДИН POST с
полем ``action``: они меняют одно и то же обращение, в одной форме, и четыре
отдельные ручки отличались бы только строкой статуса.

Два рубежа доступа
------------------
Гейт (``app/web/middleware/auth_gate.py``) не пускает не-владельца в
``/settings/*``, кроме явного member-списка, куда этот путь не входит.
Сверху — свой :func:`_require_owner` с fail-closed резолвом: в ящике лежат
email посторонних людей, и полагаться на один рубеж тут нельзя.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.support import notify, repository
from app.web.templates_engine import templates

router = APIRouter(tags=["settings", "support"])
log = get_logger("persona.support.inbox")

_PAGE = "/settings/support"
#: Потолок длины ответа. Тот же порядок, что у обращения: письмо на 200 КБ —
#: не ответ, а способ уронить SMTP-релей.
_REPLY_MAX = 4000


async def _require_owner(user_id: int, action: str) -> None:
    """403, если вызывающий не владелец. Защита в глубину поверх гейта."""
    from app.web.routes.owner_view import viewer_is_owner  # noqa: PLC0415

    if not await viewer_is_owner(int(user_id)):
        log.warning("support.owner_only_denied", user_id=int(user_id), action=action)
        raise HTTPException(
            status_code=403,
            detail="Ящик поддержки доступен только владельцу инстанса.",
        )


@router.get("/settings/support", response_class=HTMLResponse)
async def inbox(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    status: str = "",
    ticket: int = 0,
    done: str = "",
) -> HTMLResponse:
    """Лента обращений (+ раскрытое обращение, если задан ``?ticket=``)."""
    await _require_owner(int(session["user_id"]), "inbox")

    active = status if status in repository.STATUSES else ""
    tickets = await repository.list_tickets(active or None)

    open_ticket: dict[str, Any] | None = None
    messages: list[dict[str, Any]] = []
    reply_to = ""
    if ticket:
        open_ticket = await repository.get_ticket(int(ticket))
        if open_ticket is not None:
            messages = await repository.list_messages(int(ticket))
            # Адрес для ответа: у залогиненного автора берём АКТУАЛЬНЫЙ из
            # аккаунта (он мог смениться), у анонима — тот, что он оставил.
            reply_to = (
                await repository.user_email(open_ticket.get("user_id"))
                or str(open_ticket.get("email") or "")
            )
            # Открыл — значит прочитал. Молча двигаем только 'new', чтобы не
            # откатывать вручную выставленные 'answered'/'closed'.
            if str(open_ticket.get("status")) == "new":
                await repository.set_status(int(ticket), "read")
                open_ticket["status"] = "read"

    from app.smtp_delivery import delivery_status  # noqa: PLC0415

    try:
        mail_status = await delivery_status()
    except Exception as exc:  # noqa: BLE001 — ящик открывается всегда
        log.info("support.mail_status_failed", error=str(exc))
        mail_status = "unknown"

    return templates.TemplateResponse(
        request,
        "support_inbox.html",
        {
            "title": "Поддержка",
            "active_nav": "settings",
            "tickets": tickets,
            "counts": await repository.status_counts(),
            "statuses": repository.STATUSES,
            "active_status": active,
            "open_ticket": open_ticket,
            "messages": messages,
            "reply_to": reply_to,
            "mail_ok": mail_status == "ok",
            "mail_status": mail_status,
            "reply_max": _REPLY_MAX,
            "done": done,
        },
    )


@router.post("/settings/support", response_model=None)
async def inbox_action(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    """Одно действие над одним обращением: смена статуса или ответ.

    Ответ СНАЧАЛА пишется в ``support_message`` и только потом отправляется
    письмом. Если почта сломана (сейчас — сломана), текст всё равно сохранён,
    а исход доставки виден на самом сообщении. Владельцу при этом показывается
    адрес автора, чтобы он мог ответить из своего почтового клиента.
    """
    await _require_owner(int(session["user_id"]), "action")
    form = await request.form()
    ticket_id = int(str(form.get("ticket_id") or "0") or 0)
    action = str(form.get("action") or "").strip()

    ticket = await repository.get_ticket(ticket_id) if ticket_id else None
    if ticket is None:
        return RedirectResponse(url=_PAGE, status_code=303)

    if action == "delete":
        await repository.delete_ticket(ticket_id)
        log.info("support.ticket_deleted", ticket_id=ticket_id)
        return RedirectResponse(url=f"{_PAGE}?done=deleted", status_code=303)

    if action in repository.STATUSES:
        await repository.set_status(ticket_id, action)
        return RedirectResponse(
            url=f"{_PAGE}?ticket={ticket_id}&done=status", status_code=303
        )

    if action != "reply":
        return RedirectResponse(url=f"{_PAGE}?ticket={ticket_id}", status_code=303)

    body = str(form.get("reply") or "").strip()[:_REPLY_MAX]
    if not body:
        return RedirectResponse(
            url=f"{_PAGE}?ticket={ticket_id}&done=empty", status_code=303
        )

    message_id = await repository.add_message(ticket_id, body)
    to_addr = (
        await repository.user_email(ticket.get("user_id"))
        or str(ticket.get("email") or "")
    )
    try:
        outcome = await notify.reply_to_author(ticket, to_addr, body)
    except Exception as exc:  # noqa: BLE001 — ответ уже сохранён, письмо — сверху
        log.warning("support.reply_mail_failed", error=str(exc), ticket=ticket_id)
        outcome = "error:непредвиденный сбой отправки"
    await repository.set_message_delivery(message_id, outcome)
    # Ответил — значит обращение отвечено. Владелец может закрыть его отдельно.
    await repository.set_status(ticket_id, "answered")
    return RedirectResponse(
        url=f"{_PAGE}?ticket={ticket_id}&done="
        + ("sent" if outcome == "sent" else "saved"),
        status_code=303,
    )


@router.get("/api/support/unread.json")
async def unread_json(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Счётчик для бейджа в шапке. Только для владельца.

    Отдельная ручка, а не поле в существующем опросе: те опросы принадлежат
    участнику (его личные уведомления), а этот счётчик — инстанс-глобальный и
    виден одному человеку. Подмешивать owner-данные в member-ответ значило бы
    заводить в нём ветку «а если владелец» ровно того рода, из-за которой
    потом и утекает лишнее.
    """
    await _require_owner(int(session["user_id"]), "badge")
    counts = await repository.status_counts()
    return JSONResponse(
        {
            "ok": True,
            "new": counts["new"],
            "open": counts["new"] + counts["read"],
            "total": counts["total"],
            "url": _PAGE,
        }
    )
