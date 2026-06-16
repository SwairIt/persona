"""Локальные интеграции — /settings/integrations (ROADMAP S4a).

Хаб экспорта данных Persona в открытые форматы (local-first, без vendor-lock):
напоминания-задачи → .ics (iCalendar) для Apple/Google/Outlook. Здесь же —
ссылки на уже существующие календарные фиды (AI-напоминания, активность, фокус).
Скачивание идёт под owner-сессией; файл формируется локально и не уходит наружу.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, Response

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.reminders_ics import build_todo_ics
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])
log = get_logger("persona.integrations")


async def _counts() -> dict[str, int]:
    async with get_connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM reminders WHERE done = 0")
        active = int((await cur.fetchone())["n"])
    return {"active_reminders": active}


@router.get("/settings/integrations", response_class=HTMLResponse)
async def integrations_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "integrations_settings.html",
        {
            "title": "Локальные интеграции",
            "active_nav": "settings",
            "counts": await _counts(),
        },
    )


@router.get("/settings/integrations/reminders.ics", response_model=None)
async def reminders_ics_download(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    include_done: bool = False,
) -> Response:
    """Скачать напоминания-задачи как iCalendar (.ics)."""
    host = request.url.hostname or "persona.local"
    ics = await build_todo_ics(host, include_done=include_done)
    return Response(
        content=ics,
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="persona-todo.ics"'},
    )


__all__ = ["router"]
