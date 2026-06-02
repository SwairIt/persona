"""Daily time-sheet — how many minutes you spent in each app today."""

from __future__ import annotations

from datetime import date as date_cls
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.analysis import compute_per_app_seconds, format_duration
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["timesheet"])


@router.get("/timesheet", response_class=HTMLResponse)
async def timesheet_page(
    request: Request,
    date: str | None = Query(default=None),
) -> HTMLResponse:
    target = _parse_date(date)
    async with get_connection() as conn:
        items = await compute_per_app_seconds(conn, day=target)
    total_seconds = sum(item.seconds for item in items)
    return templates.TemplateResponse(
        request,
        "timesheet.html",
        {
            "title": f"Time-sheet · {target.isoformat()}",
            "active_nav": "timesheet",
            "target": target,
            "prev_day": target - timedelta(days=1),
            "next_day": target + timedelta(days=1),
            "items": [
                {
                    "app": item.app_name,
                    "seconds": item.seconds,
                    "duration": format_duration(item.seconds),
                    "percent": (item.seconds / total_seconds * 100) if total_seconds else 0,
                }
                for item in items
            ],
            "total_seconds": total_seconds,
            "total_duration": format_duration(total_seconds),
        },
    )


def _parse_date(value: str | None) -> date_cls:
    if not value:
        return datetime.now().astimezone().date()
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return datetime.now().astimezone().date()
