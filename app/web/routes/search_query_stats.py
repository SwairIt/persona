"""Search query usage stats — ``/stats/search-queries`` HTML page.

v1.4 surfaces :func:`app.search_query_stats.top_queries` as a Tailwind
table: query, run count, last-used timestamp, sorted by count desc.

A thin presentation shell — all SQL lives in
:mod:`app.search_query_stats`. The route only formats the ``last_used``
ISO timestamp for display and clamps the query-string parameters so a
bad caller can't blow up the SQL.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.search_query_stats import top_queries
from app.web.templates_engine import templates

log = get_logger("persona.search.query_stats")

router = APIRouter(tags=["stats"])

# FastAPI's ``Query`` validator surfaces a 422 before any SQL runs.
# Mirrors the clamps inside :func:`top_queries` so the API and the SQL
# layer share one source of truth on what a sane caller can ask for.
_MIN_DAYS = 1
_MAX_DAYS = 365
_DEFAULT_DAYS = 30

_MIN_LIMIT = 1
_MAX_LIMIT = 50
_DEFAULT_LIMIT = 50


def _format_timestamp(iso_value: str) -> str:
    """Render the ``last_used_at`` ISO string as ``YYYY-MM-DD HH:MM:SS``.

    Returns the raw value unchanged when ``fromisoformat`` rejects it —
    the column is ``NOT NULL`` with a SQLite ``datetime('now')`` default,
    so a parse failure means somebody hand-wrote a row and we should not
    hide the evidence by showing an em-dash.
    """
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError:
        log.warning("search.query_stats.bad_timestamp", value=iso_value)
        return iso_value
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


@router.get("/stats/search-queries", response_class=HTMLResponse)
async def search_query_stats_page(
    request: Request,
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=_MIN_LIMIT, le=_MAX_LIMIT),
) -> HTMLResponse:
    """Render the top-queries Tailwind table."""
    rows = await top_queries(days=days, limit=limit)

    decorated: list[dict[str, Any]] = [
        {
            "query": row["query"],
            "count": row["count"],
            "last_used": row["last_used"],
            "last_used_display": _format_timestamp(row["last_used"]),
        }
        for row in rows
    ]

    return templates.TemplateResponse(
        request,
        "search_query_stats.html",
        {
            "title": "Search query stats",
            "active_nav": "stats",
            "days": days,
            "limit": limit,
            "min_days": _MIN_DAYS,
            "max_days": _MAX_DAYS,
            "min_limit": _MIN_LIMIT,
            "max_limit": _MAX_LIMIT,
            "rows": decorated,
        },
    )
