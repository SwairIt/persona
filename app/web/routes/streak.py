"""Daily-capture-streak badge — HTML page and JSON endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.streak import current_streak
from app.web.templates_engine import templates

log = get_logger("persona.streak")

router = APIRouter(tags=["streak"])


@router.get("/streak", response_class=HTMLResponse)
async def streak_page(request: Request) -> HTMLResponse:
    payload = await current_streak()
    captured_today = payload["today_count"] > 0
    return templates.TemplateResponse(
        request,
        "streak.html",
        {
            "title": "Streak",
            "active_nav": "stats",
            "days": payload["days"],
            "longest": payload["longest"],
            "last_capture_date": payload["last_capture_date"],
            "today_count": payload["today_count"],
            "captured_today": captured_today,
        },
    )


@router.get("/api/streak.json", response_class=JSONResponse)
async def streak_json() -> JSONResponse:
    payload = await current_streak()
    return JSONResponse(dict(payload))
