"""Owner-only settings page for choosing which Telegram chats Persona reads,
replies in, and imports history from.

Task 1-2 of the same plan built ``telegram_chat_pref`` and made the
Telethon history importer (``pinned_ingest.sync_once``) select chats by
this table's ``ingest`` flag instead of by whether the chat happened to be
pinned in Telegram. This route is the owner's UI for that choice: it also
writes-through to ``telegram_allowed_chat_ids`` (via
``TelegramRepository.set_chat_pref``), so "может отвечать" keeps working
unchanged for the running bot.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.owner import is_owner
from app.auth.sessions import SessionRecord
from app.integrations.telegram.repository import TelegramRepository
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
_telegram = TelegramRepository()


async def _owner_id(session: SessionRecord) -> int:
    user_id = int(session["user_id"])
    if not await is_owner(user_id):
        raise HTTPException(status_code=403, detail="Только владелец")
    return user_id


async def _chat_list() -> list[dict[str, Any]]:
    known_ids = await _telegram.known_chat_ids()
    allowed_ids = await _telegram.allowed_chat_ids()
    prefs = {pref["telegram_chat_id"]: pref for pref in await _telegram.list_chat_prefs()}
    counts = await _telegram.chat_message_counts()

    all_ids = known_ids | allowed_ids | set(prefs.keys())
    chats: list[dict[str, Any]] = []
    for chat_id in all_ids:
        pref = prefs.get(chat_id)
        chats.append(
            {
                "chat_id": chat_id,
                "title": (pref["title"] if pref else "") or "",
                "message_count": counts.get(chat_id, 0),
                "mode": pref["mode"] if pref else ("reply" if chat_id in allowed_ids else "read"),
                "ingest": pref["ingest"] if pref else False,
            }
        )
    chats.sort(key=lambda item: (-item["message_count"], item["chat_id"]))
    return chats


@router.get("/settings/telegram-chats", response_class=HTMLResponse)
async def telegram_chats_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    await _owner_id(session)
    return templates.TemplateResponse(
        request,
        "telegram_chats.html",
        {
            "title": "Чаты Telegram",
            "active_nav": "settings",
            "chats": await _chat_list(),
        },
    )


@router.post("/settings/telegram-chats/{chat_id}")
async def telegram_chats_save(
    chat_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    title: str = Form(""),
    mode: str = Form("read"),
    ingest: str = Form(""),
) -> RedirectResponse:
    await _owner_id(session)
    known_ids = await _telegram.known_chat_ids()
    allowed_ids = await _telegram.allowed_chat_ids()
    existing_pref = await _telegram.chat_pref(chat_id)
    if chat_id not in known_ids and chat_id not in allowed_ids and existing_pref is None:
        raise HTTPException(status_code=404, detail="Неизвестный Telegram-чат")
    if mode not in {"reply", "read", "ignore"}:
        raise HTTPException(status_code=400, detail="Некорректный режим")
    await _telegram.set_chat_pref(
        chat_id,
        mode=mode,
        ingest=ingest == "on",
        title=title,
    )
    return RedirectResponse("/settings/telegram-chats", status_code=303)


__all__ = ["router"]
