"""Global calendar-navigator data endpoint.

A tiny JSON feed that powers the floating month-mini-calendar widget
(``app/web/static/cal_nav.js``).  The widget lives in ``base.html`` so it
ships on every page; the JS lazily hits this endpoint when the user opens
the mini calendar (and again whenever they paginate to another month).

The response shape is intentionally minimal::

    {"month": "2026-06", "days": [{"date": "2026-06-01", "count": 42}, ...]}

Only days that actually have screenshots are returned — the JS treats
absent dates as zero.
"""

from __future__ import annotations

import calendar as _calendar
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from app.storage.db import get_connection

router = APIRouter(tags=["cal-nav"])

_MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")


def _parse_month(month: str | None) -> tuple[int, int]:
    """Parse a ``YYYY-MM`` query parameter, defaulting to the current month.

    The strictness here matters: the value flows straight into a SQL
    ``WHERE captured_at >= ? AND captured_at < ?`` filter, so we refuse
    anything that doesn't match ``\\d{4}-\\d{2}`` plus a sanity range check.
    """
    if not month:
        now = datetime.now(timezone.utc)
        return now.year, now.month

    m = _MONTH_RE.match(month)
    if not m:
        raise HTTPException(status_code=400, detail="month must be YYYY-MM")

    year = int(m.group(1))
    mon = int(m.group(2))
    if year < 1970 or year > 9999 or mon < 1 or mon > 12:
        raise HTTPException(status_code=400, detail="month out of range")
    return year, mon


@router.get("/api/cal-nav-days.json", response_class=JSONResponse)
async def cal_nav_days(month: str | None = Query(default=None)) -> JSONResponse:
    """Return ``{date, count}`` rows for every populated day in ``month``."""
    year, mon = _parse_month(month)

    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    last_day = _calendar.monthrange(year, mon)[1]
    if mon == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, mon + 1, 1, tzinfo=timezone.utc)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DATE(captured_at) AS day, COUNT(*) AS n "
            "FROM screenshots "
            "WHERE captured_at >= ? AND captured_at < ? "
            "GROUP BY day "
            "ORDER BY day",
            (start.isoformat(), end.isoformat()),
        )
        rows = await cursor.fetchall()

    days = [{"date": str(row["day"]), "count": int(row["n"])} for row in rows]

    return JSONResponse(
        {
            "month": f"{year:04d}-{mon:02d}",
            "first_day": f"{year:04d}-{mon:02d}-01",
            "last_day": f"{year:04d}-{mon:02d}-{last_day:02d}",
            "days": days,
        }
    )
