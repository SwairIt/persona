"""Редактор личной памяти ассистента — /settings/memory.

Trust-фича (как у Hermes): пользователь видит ВСЁ, что ИИ о нём помнит, и может
редактировать/удалять/закреплять. Данные — таблица user_memory (app/chat/user_memory.py).
Server-rendered + form-POST, работает без JS.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.chat.user_memory import (
    add_memory,
    count_memory,
    delete_memory,
    list_memory,
    set_pinned,
)
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.memory.settings")

# Мягкий бюджет (инсайт Hermes: видимый лимит дисциплинирует, не даёт свалке расти).
_BUDGET = 60


async def _render(request: Request, user_id: int, *, saved: str = "") -> HTMLResponse:
    items = await list_memory(user_id, limit=500)
    return templates.TemplateResponse(
        request,
        "memory_settings.html",
        {
            "title": "Память — что ИИ помнит обо мне",
            "active_nav": "settings",
            "items": items,
            "count": len(items),
            "budget": _BUDGET,
            "saved": saved,
        },
    )


@router.get("/settings/memory", response_class=HTMLResponse)
async def memory_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    return await _render(request, session["user_id"])


@router.post("/settings/memory/add", response_model=None)
async def memory_add(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    text: str = Form(default=""),
    kind: str = Form(default="fact"),
    pinned: str = Form(default=""),
) -> RedirectResponse:
    if text.strip():
        await add_memory(
            session["user_id"], text, kind=kind, pinned=bool(pinned)
        )
    return RedirectResponse("/settings/memory", status_code=303)


@router.post("/settings/memory/{mem_id}/delete", response_model=None)
async def memory_delete(
    mem_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    await delete_memory(session["user_id"], mem_id)
    return RedirectResponse("/settings/memory", status_code=303)


@router.post("/settings/memory/{mem_id}/pin", response_model=None)
async def memory_pin(
    mem_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    pinned: str = Form(default=""),
) -> RedirectResponse:
    await set_pinned(session["user_id"], mem_id, bool(pinned))
    return RedirectResponse("/settings/memory", status_code=303)
