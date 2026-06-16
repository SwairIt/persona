"""Локальные интеграции — /settings/integrations (ROADMAP S4a).

Хаб экспорта данных Persona в открытые форматы (local-first, без vendor-lock):
напоминания-задачи → .ics (iCalendar) для Apple/Google/Outlook. Здесь же —
ссылки на уже существующие календарные фиды (AI-напоминания, активность, фокус).
Скачивание идёт под owner-сессией; файл формируется локально и не уходит наружу.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.chat.markdown_import import import_markdown
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
    imported: int | None = None,
    parsed: int | None = None,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "integrations_settings.html",
        {
            "title": "Локальные интеграции",
            "active_nav": "settings",
            "counts": await _counts(),
            "imported": imported,
            "parsed": parsed,
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


@router.post("/settings/integrations/import-markdown", response_model=None)
async def import_markdown_route(
    session: Annotated[SessionRecord, Depends(current_user_required)],
    text: str = Form(default=""),
    file: Annotated[UploadFile | None, File()] = None,
) -> RedirectResponse:
    """Импорт .md (вставка или файл) → факты в память."""
    md = text or ""
    if file is not None:
        try:
            raw = await file.read()
            md = (md + "\n" + raw.decode("utf-8", errors="replace")).strip()
        except Exception as exc:  # noqa: BLE001
            log.warning("integrations.md_read_failed", error=str(exc))
    stats = {"parsed": 0, "added": 0}
    if md.strip():
        try:
            stats = await import_markdown(session["user_id"], md)
            log.info("integrations.md_import", **stats)
        except Exception as exc:  # noqa: BLE001
            log.warning("integrations.md_import_failed", error=str(exc))
    return RedirectResponse(
        f"/settings/integrations?imported={stats['added']}&parsed={stats['parsed']}",
        status_code=303,
    )


__all__ = ["router"]
