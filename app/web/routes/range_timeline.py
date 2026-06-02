"""Custom date-range timeline view — browse captures over an arbitrary window."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.storage.db import get_connection
from app.storage.repository import list_screenshots
from app.storage.tags import get_tags_for_many
from app.web.templates_engine import templates

router = APIRouter(tags=["range-timeline"])

MAX_RANGE_DAYS = 90
MAX_SHOTS = 1000


@router.get("/range", response_class=HTMLResponse)
async def range_timeline(
    request: Request,
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
) -> HTMLResponse:
    """Render captures between `since` and `until` (inclusive of both days)."""
    today = _today()

    since_date = _parse_date_or_default(since, default=today - timedelta(days=6))
    until_date = _parse_date_or_default(until, default=today)

    # Swap silently if reversed.
    if since_date > until_date:
        since_date, until_date = until_date, since_date

    days_count = (until_date - since_date).days + 1
    if days_count > MAX_RANGE_DAYS:
        raise HTTPException(
            status_code=400,
            detail=f"Range too large: {days_count} days (max {MAX_RANGE_DAYS}).",
        )

    since_dt, until_dt = _range_bounds_utc(since_date, until_date)

    async with get_connection() as conn:
        shots = await list_screenshots(
            conn,
            limit=MAX_SHOTS,
            since=since_dt,
            until=until_dt,
        )
        tags_by_id = await get_tags_for_many(conn, [s.id for s in shots])

    presets = _build_presets(today)

    return templates.TemplateResponse(
        request,
        "range_timeline.html",
        {
            "title": "Range",
            "active_nav": "search",
            "since": since_date.strftime("%Y-%m-%d"),
            "until": until_date.strftime("%Y-%m-%d"),
            "days_count": days_count,
            "total": len(shots),
            "shots": shots,
            "tags_by_id": tags_by_id,
            "presets": presets,
        },
    )


def _today() -> date:
    return datetime.now().astimezone().date()


def _parse_date_or_default(value: str | None, *, default: date) -> date:
    """Parse a YYYY-MM-DD string, or return the default if absent.

    Raises HTTPException(400) if a value is present but unparseable.
    """
    if value is None or value == "":
        return default
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date '{value}', expected YYYY-MM-DD.",
        ) from exc


def _range_bounds_utc(since_date: date, until_date: date) -> tuple[datetime, datetime]:
    """Translate inclusive date range to (since_dt, until_dt) in UTC.

    `until_dt` is midnight of (until + 1 day) so the `captured_at < until_dt`
    filter in list_screenshots still includes the entire `until` day.
    """
    tz = datetime.now().astimezone().tzinfo
    since_local = datetime(since_date.year, since_date.month, since_date.day, tzinfo=tz)
    until_exclusive = datetime(
        until_date.year, until_date.month, until_date.day, tzinfo=tz
    ) + timedelta(days=1)
    return since_local.astimezone(timezone.utc), until_exclusive.astimezone(timezone.utc)


def _build_presets(today: date) -> list[dict[str, Any]]:
    """Build the quick-preset link metadata server-side."""
    last_7_since = today - timedelta(days=6)
    last_30_since = today - timedelta(days=29)

    # ISO week — Monday is day 0.
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    presets: list[dict[str, Any]] = [
        {
            "label": "Last 7 days",
            "since": last_7_since.strftime("%Y-%m-%d"),
            "until": today.strftime("%Y-%m-%d"),
        },
        {
            "label": "Last 30 days",
            "since": last_30_since.strftime("%Y-%m-%d"),
            "until": today.strftime("%Y-%m-%d"),
        },
        {
            "label": "This week",
            "since": week_start.strftime("%Y-%m-%d"),
            "until": today.strftime("%Y-%m-%d"),
        },
        {
            "label": "This month",
            "since": month_start.strftime("%Y-%m-%d"),
            "until": today.strftime("%Y-%m-%d"),
        },
    ]
    return presets
