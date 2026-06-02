"""Routes for analysis features — diff between two screenshots and sessions view."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.analysis import build_sessions, diff_screenshots
from app.storage.db import get_connection
from app.storage.repository import get_screenshot, list_screenshots
from app.web.templates_engine import templates

router = APIRouter(tags=["analysis"])


@router.get("/diff", response_class=HTMLResponse)
async def diff_page(
    request: Request,
    left: int = Query(...),
    right: int = Query(...),
) -> HTMLResponse:
    async with get_connection() as conn:
        left_shot = await get_screenshot(conn, left)
        right_shot = await get_screenshot(conn, right)
    if left_shot is None or right_shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    result = diff_screenshots(
        left_ocr=left_shot.ocr_text,
        right_ocr=right_shot.ocr_text,
        left_phash=left_shot.phash,
        right_phash=right_shot.phash,
    )

    return templates.TemplateResponse(
        request,
        "diff.html",
        {
            "title": f"Diff #{left} vs #{right}",
            "active_nav": "timeline",
            "left": left_shot,
            "right": right_shot,
            "diff": result,
        },
    )


@router.get("/sessions", response_class=HTMLResponse)
async def sessions_page(
    request: Request,
    date: str | None = Query(default=None),
) -> HTMLResponse:
    target = _parse_date(date)
    since = target.astimezone(timezone.utc)
    until = since + timedelta(days=1)

    async with get_connection() as conn:
        shots = await list_screenshots(conn, since=since, until=until, limit=5000)

    sessions = build_sessions(shots)

    return templates.TemplateResponse(
        request,
        "sessions.html",
        {
            "title": "Sessions",
            "active_nav": "timeline",
            "target_day": target,
            "sessions": sessions,
        },
    )


def _parse_date(value: str | None) -> datetime:
    if not value:
        now = datetime.now().astimezone()
        return datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)
    parsed = datetime.strptime(value, "%Y-%m-%d")
    return parsed.replace(tzinfo=datetime.now().astimezone().tzinfo or timezone.utc)
