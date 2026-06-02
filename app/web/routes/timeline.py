"""Timeline view — newest captures grouped by hour."""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.storage.db import get_connection
from app.storage.models import Screenshot
from app.storage.repository import list_screenshots
from app.web.templates_engine import templates

router = APIRouter(tags=["timeline"])


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    date: str | None = Query(default=None),
    app: str | None = Query(default=None),
) -> HTMLResponse | RedirectResponse:
    """Render the main timeline."""
    target_day = _parse_date(date)
    since, until = _day_bounds(target_day)

    async with get_connection() as conn:
        total_cursor = await conn.execute("SELECT COUNT(*) AS n FROM screenshots")
        total_row = await total_cursor.fetchone()
        total_screenshots = int(total_row["n"]) if total_row else 0
        if total_screenshots == 0 and not date:
            return RedirectResponse(url="/welcome", status_code=303)

        shots = await list_screenshots(
            conn,
            limit=500,
            since=since,
            until=until,
            app_name=app,
        )

        apps_for_day = await _day_apps(conn, since, until)

        from app.storage.tags import get_tags_for_many

        tags_by_id = await get_tags_for_many(conn, [s.id for s in shots])

    grouped = _group_by_hour(shots)

    return templates.TemplateResponse(
        request,
        "timeline.html",
        {
            "title": "Timeline",
            "active_nav": "timeline",
            "target_day": target_day,
            "prev_day": target_day - timedelta(days=1),
            "next_day": target_day + timedelta(days=1),
            "today": _today(),
            "groups": grouped,
            "total": len(shots),
            "app_filter": app,
            "apps_for_day": apps_for_day,
            "tags_by_id": tags_by_id,
        },
    )


async def _day_apps(conn: Any, since: datetime, until: datetime) -> list[tuple[str, int]]:
    """Return [(app_name, count), ...] for the given day."""
    from app.storage.time import iso

    cursor = await conn.execute(
        "SELECT app_name, COUNT(*) AS n FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? AND app_name IS NOT NULL "
        "GROUP BY app_name ORDER BY n DESC LIMIT 12",
        (iso(since), iso(until)),
    )
    rows = await cursor.fetchall()
    return [(str(row["app_name"]), int(row["n"])) for row in rows]


def _today() -> datetime:
    now = datetime.now(timezone.utc).astimezone()
    return datetime(now.year, now.month, now.day, tzinfo=now.tzinfo)


def _parse_date(value: str | None) -> datetime:
    if not value:
        return _today()
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return _today()
    tz = datetime.now().astimezone().tzinfo
    return parsed.replace(tzinfo=tz)


def _day_bounds(day: datetime) -> tuple[datetime, datetime]:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _group_by_hour(shots: list[Screenshot]) -> OrderedDict[str, list[Screenshot]]:
    out: OrderedDict[str, list[Screenshot]] = OrderedDict()
    for shot in shots:
        local = shot.captured_at.astimezone()
        key = local.strftime("%H:00")
        out.setdefault(key, []).append(shot)
    return out
