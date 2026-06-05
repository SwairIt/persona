"""Long-read bookmarks — HTML page + JSON API.

v1.39 feature. Exposes the auto-bookmarks written by
:func:`app.long_read_detector.detect_long_reads`:

* ``GET /long-reads`` — Tailwind table of recent long-read sessions
  with window title, app, duration, and links to the first/last
  bounding screenshots.
* ``GET /api/long-reads.json`` — JSON companion returning the same
  rows in machine-readable form.

This module deliberately does NOT register itself with the FastAPI
app in :mod:`app.web.main`; it is wired by a follow-up patch with::

    from app.web.routes import long_reads as long_reads_routes
    app.include_router(long_reads_routes.router)
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.web.long_reads")

router = APIRouter(tags=["long-reads"])

_DEFAULT_LIMIT: Final[int] = 100
_MAX_LIMIT: Final[int] = 500


def _format_duration(seconds: int) -> str:
    """Render ``duration_seconds`` as a compact ``Hh Mm`` / ``Mm`` string."""
    if seconds < 60:
        return f"{seconds}s"
    minutes, rem_seconds = divmod(int(seconds), 60)
    hours, rem_minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {rem_minutes}m"
    return f"{rem_minutes}m {rem_seconds}s" if rem_seconds and minutes < 5 else f"{minutes}m"


async def _fetch_recent(limit: int) -> list[dict[str, Any]]:
    """Return up to ``limit`` ``long_read`` rows ordered by ``started_at DESC``."""
    bounded = max(1, min(limit, _MAX_LIMIT))
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, window_title, app_name, started_at, ended_at, "
            "duration_seconds, screenshot_id_first, screenshot_id_last, "
            "created_at "
            "FROM long_read "
            "ORDER BY started_at DESC "
            "LIMIT ?",
            (bounded,),
        )
        rows = await cursor.fetchall()

    items: list[dict[str, Any]] = []
    for row in rows:
        duration_raw = row["duration_seconds"]
        duration_seconds = int(duration_raw) if duration_raw is not None else 0
        first_id_raw = row["screenshot_id_first"]
        last_id_raw = row["screenshot_id_last"]
        items.append(
            {
                "id": int(row["id"]),
                "window_title": str(row["window_title"]),
                "app_name": str(row["app_name"]) if row["app_name"] is not None else None,
                "started_at": str(row["started_at"]),
                "ended_at": str(row["ended_at"]),
                "duration_seconds": duration_seconds,
                "duration_label": _format_duration(duration_seconds),
                "screenshot_id_first": int(first_id_raw) if first_id_raw is not None else None,
                "screenshot_id_last": int(last_id_raw) if last_id_raw is not None else None,
                "created_at": str(row["created_at"]) if row["created_at"] is not None else None,
            }
        )
    return items


@router.get("/long-reads", response_class=HTMLResponse)
async def long_reads_page(
    request: Request,
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> HTMLResponse:
    """Render the recent long-read bookmarks table."""
    items = await _fetch_recent(limit)
    log.info("long_reads.page", count=len(items), limit=limit)
    return templates.TemplateResponse(
        request,
        "long_reads.html",
        {
            "title": "Long reads",
            "active_nav": "memory",
            "items": items,
            "limit": limit,
        },
    )


@router.get("/api/long-reads.json", response_class=JSONResponse)
async def long_reads_json(
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> JSONResponse:
    """Machine-readable companion to :func:`long_reads_page`."""
    items = await _fetch_recent(limit)
    return JSONResponse({"items": items, "count": len(items), "limit": limit})


__all__ = ["router"]
