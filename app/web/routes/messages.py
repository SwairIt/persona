"""Личные сообщения — /messages + /api/messages/*.

Список переписок, сама переписка (пузыри «моё/его», метка «✨ ответил ИИ»
для ``kind='ai'``, дозагрузка старых), отправка, polling новых и счётчик
непрочитанного для бейджа в навбаре.

Почему polling, а не SSE: переписка двух людей — это единицы сообщений в
минуту. Чатовая SSE-машинерия (``app/web/routes/chat_sessions.py``) заметно
тяжелее, чем тут нужно, а собственную стриминг-инфраструктуру ради бейджа
строить незачем.

Доступ к ветке резолвится ОДНИМ резолвером в репозитории
(``_require_thread_member``): он же проверяет и участие, и то, что люди
всё ещё друзья. Промах — :class:`ThreadAccessError` → 404 (не 403: «нет
такой ветки» не подтверждает существование чужой переписки, поэтому
перебор id ничего не даёт).
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.social.ai_reply import dispatch as dispatch_ai_reply
from app.social.repository import (
    MAX_MESSAGE_CHARS,
    SocialError,
    ThreadAccessError,
    get_or_create_thread,
    list_messages,
    list_threads,
    mark_read,
    send_message,
    thread_header,
    unread_total,
)
from app.web.rate_limit import allow
from app.web.templates_engine import templates

router = APIRouter(tags=["social"])
log = get_logger("persona.social.messages")

# Страница переписки за раз показывает столько сообщений; «показать раньше»
# докидывает следующую такую же пачку.
_PAGE_SIZE = 30
# Отправка: 60 сообщений в минуту на аккаунт — человеку хватит, флудеру нет.
_SEND_MAX_EVENTS = 60
_SEND_WINDOW_SECONDS = 60

_NOT_FOUND = {"error": "переписка не найдена"}


@router.get("/messages", response_class=HTMLResponse)
async def messages_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    uid = int(session["user_id"])
    return templates.TemplateResponse(
        request,
        "messages.html",
        {
            "title": "Сообщения",
            "active_nav": "messages",
            "threads": await list_threads(uid),
        },
    )


@router.get("/messages/with/{user_id}", response_model=None)
async def messages_open_with(
    user_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    """«Написать» со страницы друзей: завести/найти ветку и уйти в неё."""
    try:
        thread_id = await get_or_create_thread(int(session["user_id"]), user_id)
    except ThreadAccessError:
        return RedirectResponse("/friends", status_code=303)
    return RedirectResponse(f"/messages/{thread_id}", status_code=303)


@router.get("/messages/{thread_id}", response_model=None)
async def thread_page(
    thread_id: int,
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse | RedirectResponse:
    uid = int(session["user_id"])
    try:
        header = await thread_header(thread_id, uid)
        messages = await list_messages(thread_id, uid, limit=_PAGE_SIZE)
        await mark_read(thread_id, uid)
    except ThreadAccessError:
        # Чужая/несуществующая ветка выглядит одинаково: просто уводим в список.
        return RedirectResponse("/messages", status_code=303)
    return templates.TemplateResponse(
        request,
        "messages_thread.html",
        {
            "title": f"Сообщения — {header['name']}",
            "active_nav": "messages",
            "thread": header,
            "messages": messages,
            "page_size": _PAGE_SIZE,
            "max_chars": MAX_MESSAGE_CHARS,
            "has_more": len(messages) >= _PAGE_SIZE,
        },
    )


@router.post("/api/messages/{thread_id}/send", response_class=JSONResponse)
async def api_send(
    thread_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    payload: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    uid = int(session["user_id"])
    if not allow(f"social:send:{uid}", _SEND_MAX_EVENTS, _SEND_WINDOW_SECONDS):
        return JSONResponse({"error": "слишком быстро, выдохни"}, status_code=429)
    try:
        message = await send_message(thread_id, uid, str(payload.get("body") or ""))
    except ThreadAccessError:
        return JSONResponse(_NOT_FOUND, status_code=404)
    except SocialError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    # Уведомление получателю + возможный ход ИИ — В ФОНЕ, как ``_bg_*`` в
    # chat_sessions.py. Отдельного воркера ради одного LLM-вызова заводить
    # незачем, а ответ отправителю не должен ждать ни SMTP, ни провайдера
    # модели. Задача сама глотает любые сбои (см. ``dispatch``).
    asyncio.create_task(  # noqa: RUF006 — fire-and-forget, результат не нужен
        dispatch_ai_reply(thread_id, int(message["id"])),
        name=f"dm-ai-reply-{thread_id}",
    )
    return JSONResponse({"ok": True, "message": message})


@router.get("/api/messages/{thread_id}/poll", response_class=JSONResponse)
async def api_poll(
    thread_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    after_id: int = 0,
) -> JSONResponse:
    """Новые сообщения ветки после ``after_id`` (и заодно отметка прочтения)."""
    uid = int(session["user_id"])
    try:
        messages = await list_messages(
            thread_id, uid, after_id=max(0, int(after_id)), limit=_PAGE_SIZE * 2
        )
        if messages:
            await mark_read(thread_id, uid)
    except ThreadAccessError:
        return JSONResponse(_NOT_FOUND, status_code=404)
    return JSONResponse({"messages": messages})


@router.get("/api/messages/{thread_id}/older", response_class=JSONResponse)
async def api_older(
    thread_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    before_id: int = 0,
) -> JSONResponse:
    """Предыдущая страница ветки (кнопка «показать раньше»)."""
    uid = int(session["user_id"])
    try:
        messages = await list_messages(
            thread_id,
            uid,
            before_id=int(before_id) if int(before_id) > 0 else None,
            limit=_PAGE_SIZE,
        )
    except ThreadAccessError:
        return JSONResponse(_NOT_FOUND, status_code=404)
    return JSONResponse({"messages": messages, "has_more": len(messages) >= _PAGE_SIZE})


@router.get("/api/messages/unread.json", response_class=JSONResponse)
async def api_unread(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    """Счётчик непрочитанного для бейджа «Сообщения» в навбаре."""
    return JSONResponse({"unread": await unread_total(int(session["user_id"]))})


__all__ = ["router"]
