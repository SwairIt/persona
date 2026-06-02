"""Month-at-a-glance calendar view."""

from __future__ import annotations

import calendar
from datetime import date, datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["calendar"])


@router.get("/calendar", response_class=HTMLResponse)
async def calendar_page(
    request: Request,
    year: int | None = Query(default=None),
    month: int | None = Query(default=None),
) -> HTMLResponse:
    today = date.today()
    y = year or today.year
    m = month or today.month

    counts = await _counts_by_day(y, m)
    cal = calendar.monthcalendar(y, m)

    prev_y, prev_m = (y - 1, 12) if m == 1 else (y, m - 1)
    next_y, next_m = (y + 1, 1) if m == 12 else (y, m + 1)

    return templates.TemplateResponse(
        request,
        "calendar.html",
        {
            "title": f"Calendar — {calendar.month_name[m]} {y}",
            "active_nav": "timeline",
            "year": y,
            "month": m,
            "month_name": calendar.month_name[m],
            "weeks": cal,
            "counts": counts,
            "today_iso": today.strftime("%Y-%m-%d"),
            "prev_year": prev_y,
            "prev_month": prev_m,
            "next_year": next_y,
            "next_month": next_m,
        },
    )


async def _counts_by_day(year: int, month: int) -> dict[str, int]:
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    if month == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, month + 1, 1, tzinfo=timezone.utc)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DATE(captured_at) AS day, COUNT(*) AS n FROM screenshots "
            "WHERE captured_at >= ? AND captured_at < ? GROUP BY day",
            (start.isoformat(), end.isoformat()),
        )
        rows = await cursor.fetchall()

    return {str(row["day"]): int(row["n"]) for row in rows}
