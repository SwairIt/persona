"""Аналитика — /analytics (BUILD_PLAN B).

Удобная сводка активности за период (7/30/90 дней): KPI, посуточные бар-чарты,
топ-приложения, использование ИИ. Server-rendered (бары — div, без тяжёлых либ),
дни кликабельны → /day/{date}. Кабинетная, под current_user_required.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from app.analytics_overview import get_analytics
from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.web.templates_engine import templates

router = APIRouter(tags=["analytics"])


@router.get("/analytics", response_class=HTMLResponse, response_model=None)
async def analytics_page(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    days: int = Query(default=30),
) -> HTMLResponse:
    data = await get_analytics(days=days, user_id=session["user_id"])
    return templates.TemplateResponse(
        request,
        "analytics.html",
        {"title": "Аналитика", "active_nav": "analytics", "a": data},
    )


__all__ = ["router"]
