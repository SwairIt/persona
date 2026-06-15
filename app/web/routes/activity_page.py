"""Окно активности ИИ — страница /activity.

Показывает, ЧТО делает ассистент: ленту вызовов инструментов (builtin/браузер/MCP)
с живым обновлением по SSE (`/events`, type=activity) и историей из
`tool_execution` (app/activity). Это «прозрачность агента» — пользователь видит
каждый шаг: какой инструмент, с какими аргументами, результат, сколько заняло.

Данные: GET /api/activity/recent (общая лента) и
GET /api/chat/activity/{session_id} (по сессии) — оба в chat_sessions.py.
Страница — тонкий Alpine-клиент над ними.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.web.templates_engine import templates

router = APIRouter(tags=["activity"])


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "activity.html",
        {
            "title": "Что делает ИИ — окно активности",
            "active_nav": "activity",
        },
    )
