"""Timeline view — newest captures grouped by hour."""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.models import Screenshot
from app.storage.repository import list_screenshots
from app.web.templates_engine import templates

log = get_logger("persona.grid_sort")

router = APIRouter(tags=["timeline"])

# Whitelist of allowed ``sort_by`` query values mapped to deterministic
# Python ``sorted()`` key functions. Server-side enforcement: any value
# outside this dict falls back to the default (``captured_at``), so no
# user input ever reaches an ORDER BY clause as raw text.
_SORT_OPTIONS: dict[str, tuple[str, bool]] = {
    # key: (attribute name on Screenshot, reverse?)
    "captured_at": ("captured_at", True),
    "captured_at_asc": ("captured_at", False),
    "app_name": ("app_name", False),
    "ocr_length": ("ocr_text", True),
}
_DEFAULT_SORT = "captured_at"


@router.get("/", response_class=HTMLResponse)
async def home(
    request: Request,
    date: str | None = Query(default=None),
    app: str | None = Query(default=None),
    sort_by: str = Query(default=_DEFAULT_SORT),
) -> HTMLResponse | RedirectResponse:
    """Render the main timeline."""
    target_day = _parse_date(date)
    since, until = _day_bounds(target_day)
    sort_key = _coerce_sort(sort_by)

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

    # ``list_screenshots`` always returns captured_at DESC. When the user
    # requested a different order we re-sort the already-bounded page in
    # Python — cheap (<=500 rows) and keeps the shared repository helper
    # untouched.
    if sort_key != _DEFAULT_SORT:
        shots = _apply_sort(shots, sort_key)
        log.info("grid_sort.timeline", sort_by=sort_key, count=len(shots))

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
            "sort_by": sort_key,
            "sort_options": _SORT_OPTIONS,
        },
    )


def _coerce_sort(value: str | None) -> str:
    """Reduce arbitrary user input to a whitelisted sort key."""
    if value and value in _SORT_OPTIONS:
        return value
    return _DEFAULT_SORT


def _apply_sort(shots: list[Screenshot], sort_key: str) -> list[Screenshot]:
    """Sort a list of Screenshots using the whitelisted key."""
    attr, reverse = _SORT_OPTIONS[sort_key]

    def key(shot: Screenshot) -> tuple[Any, Any]:
        raw = getattr(shot, attr, None)
        if sort_key == "ocr_length":
            length = len(raw) if isinstance(raw, str) else 0
            return (length, shot.captured_at)
        if raw is None:
            # Empty string sorts before any real value; combined with
            # ``reverse`` this keeps NULLs predictably grouped.
            return ("", shot.captured_at)
        return (raw, shot.captured_at)

    return sorted(shots, key=key, reverse=reverse)


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
