"""Единая страница ДНЯ — /day/{date} (BUILD_PLAN A2).

Сводит воедино то, что раньше было размазано по timeline/scrubber/collage/stats:
KPI дня (скрины, был ли записан звук, сколько использовался ИИ, часы активности),
TL;DR, топ-приложения, часовые карточки памяти и галерею скринов по часам. Отсюда
же — «спросить про этот день» (A3) и быстрые ссылки на остальные виды дня.
Owner/кабинет: под current_user_required.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.day_overview import get_day_overview
from app.storage.db import get_connection
from app.storage.repository import list_screenshots
from app.web.routes.timeline import _day_bounds, _group_by_hour, _parse_date
from app.web.templates_engine import templates

router = APIRouter(tags=["day"])

_MAX_SHOTS = 600


@router.get("/day/{date}", response_class=HTMLResponse, response_model=None)
async def day_page(
    request: Request,
    date: str,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    """Обзор одного дня: статистика + скрины по часам + карточки + действия."""
    day = _parse_date(date)  # дружелюбно: мусор → сегодня
    day_iso = day.strftime("%Y-%m-%d")
    overview = await get_day_overview(day_iso, user_id=session["user_id"])

    since, until = _day_bounds(day)
    async with get_connection() as conn:
        shots = await list_screenshots(conn, limit=_MAX_SHOTS, since=since, until=until)
        tags_by_id: dict = {}
        try:
            from app.storage.tags import get_tags_for_many  # noqa: PLC0415

            tags_by_id = await get_tags_for_many(conn, [s.id for s in shots])
        except Exception:  # noqa: BLE001 — теги не критичны для страницы дня
            tags_by_id = {}

    groups = _group_by_hour(shots)
    return templates.TemplateResponse(
        request,
        "day_overview.html",
        {
            "title": f"День · {day_iso}",
            "active_nav": "day",
            "ov": overview,
            "day_iso": day_iso,
            "groups": groups,
            "tags_by_id": tags_by_id,
            "shots_shown": len(shots),
            "shots_capped": len(shots) >= _MAX_SHOTS,
        },
    )


__all__ = ["router"]
