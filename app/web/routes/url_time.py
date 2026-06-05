"""URL-time dashboard — HTML page + JSON API (v1.50).

Two endpoints:

* ``GET /stats/url-time`` — HTML page rendering the top 30 page labels
  (by estimated seconds) across the last seven local days.
* ``GET /api/stats/url-time.json?day=YYYY-MM-DD`` — JSON view of a
  single day's aggregates.  Defaults to today on missing / bad input.

The page never reaches into the screenshots table directly — it only
reads the pre-aggregated ``url_time_aggregate`` rows produced by
:mod:`app.workers.url_time_worker`.  If the worker hasn't run yet
the page renders an empty list rather than blocking on an ad-hoc
aggregate.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.web.url_time")

router = APIRouter(tags=["url-time"])

_TOP_N: int = 30
_LOOKBACK_DAYS: int = 7


def _format_hms(seconds: int) -> str:
    """Format ``seconds`` as ``H:MM:SS``."""
    safe = max(int(seconds), 0)
    minutes, sec = divmod(safe, 60)
    hours, mins = divmod(minutes, 60)
    return f"{hours}:{mins:02d}:{sec:02d}"


def _parse_day(value: str | None) -> date:
    """Parse ``YYYY-MM-DD``; fall back to today on bad input."""
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            log.warning("url_time.bad_day_param", day=value)
    return datetime.now().astimezone().date()


async def _top_for_window(days: int, limit: int) -> list[dict[str, object]]:
    """SELECT the top ``limit`` (browser, page_label) by est_seconds.

    Aggregation is done in SQL so the route stays a thin formatter.
    """
    today = datetime.now().astimezone().date()
    start_day = today - timedelta(days=days - 1)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT browser, page_label, "
            "       SUM(screen_count) AS screen_count, "
            "       SUM(est_seconds) AS est_seconds "
            "FROM url_time_aggregate "
            "WHERE day >= ? AND day <= ? "
            "GROUP BY browser, page_label "
            "ORDER BY est_seconds DESC, screen_count DESC "
            "LIMIT ?",
            (start_day.isoformat(), today.isoformat(), limit),
        )
        rows = await cursor.fetchall()

    items: list[dict[str, object]] = []
    for row in rows:
        est_seconds = int(row["est_seconds"] or 0)
        items.append(
            {
                "browser": str(row["browser"]),
                "page_label": str(row["page_label"]),
                "screen_count": int(row["screen_count"] or 0),
                "est_seconds": est_seconds,
                "duration": _format_hms(est_seconds),
            }
        )
    return items


def _decorate(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """Add a ``percent`` bar width relative to the busiest label."""
    if not items:
        return []
    max_sec = max((int(i["est_seconds"]) for i in items), default=0)  # type: ignore[call-overload]
    decorated: list[dict[str, object]] = []
    for item in items:
        sec = int(item["est_seconds"])  # type: ignore[call-overload]
        pct = (sec / max_sec * 100.0) if max_sec else 0.0
        decorated.append({**item, "percent": pct})
    return decorated


@router.get("/stats/url-time", response_class=HTMLResponse)
async def url_time_page(request: Request) -> HTMLResponse:
    """Render the top-30 page labels for the trailing seven days."""
    raw_items = await _top_for_window(days=_LOOKBACK_DAYS, limit=_TOP_N)
    items = _decorate(raw_items)
    total_seconds = sum(int(i["est_seconds"]) for i in raw_items)  # type: ignore[call-overload]
    today = datetime.now().astimezone().date()
    start_day = today - timedelta(days=_LOOKBACK_DAYS - 1)

    return templates.TemplateResponse(
        request,
        "url_time.html",
        {
            "title": "Время на страницах",
            "active_nav": "stats",
            "days": _LOOKBACK_DAYS,
            "start_day": start_day.isoformat(),
            "end_day": today.isoformat(),
            "today_iso": today.isoformat(),
            "items": items,
            "total_seconds": total_seconds,
            "total_duration": _format_hms(total_seconds),
        },
    )


@router.get("/api/stats/url-time.json", response_class=JSONResponse)
async def url_time_json(day: str | None = Query(default=None)) -> JSONResponse:
    """JSON dump of a single day's URL-time aggregates."""
    target = _parse_day(day)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT browser, page_label, screen_count, est_seconds, computed_at "
            "FROM url_time_aggregate "
            "WHERE day = ? "
            "ORDER BY est_seconds DESC, screen_count DESC",
            (target.isoformat(),),
        )
        rows = await cursor.fetchall()

    items: list[dict[str, object]] = []
    total_seconds = 0
    for row in rows:
        est_seconds = int(row["est_seconds"] or 0)
        total_seconds += est_seconds
        items.append(
            {
                "browser": str(row["browser"]),
                "page_label": str(row["page_label"]),
                "screen_count": int(row["screen_count"] or 0),
                "est_seconds": est_seconds,
                "duration": _format_hms(est_seconds),
                "computed_at": str(row["computed_at"]),
            }
        )

    return JSONResponse(
        {
            "day": target.isoformat(),
            "total_seconds": total_seconds,
            "total_duration": _format_hms(total_seconds),
            "items": items,
        }
    )
