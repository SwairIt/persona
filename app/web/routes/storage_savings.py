"""``/stats/storage-savings`` — per-day storage-savings chart + table.

Pairs with :mod:`app.storage_savings`. The HTML route renders a 30-day
SVG line chart of bytes reclaimed by the three housekeeping passes
(pHash dedup, on-disk thumb dedup, recycle-bin retention purge) plus a
per-day breakdown table. The JSON route returns the same payload
unwrapped so a dashboard or external consumer can poll it directly.

``days`` is read from the query string and clamped inside
:func:`app.storage_savings.chart_data` so an out-of-range value cannot
ask for unbounded history.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage_savings import chart_data
from app.web.templates_engine import templates

router = APIRouter(tags=["storage-savings"])
log = get_logger("persona.savings")

_DEFAULT_DAYS = 30
_MAX_DAYS = 365


@router.get("/stats/storage-savings", response_class=HTMLResponse)
async def storage_savings_page(
    request: Request,
    days: int = Query(_DEFAULT_DAYS, ge=1, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the savings chart + table for the last ``days`` days."""
    rows = await chart_data(days=days)

    grand_bytes = sum(row["bytes_saved"] for row in rows)
    grand_dedup_hits = sum(row["dedup_hits"] for row in rows)
    grand_thumb_bytes = sum(row["thumb_dedup_bytes"] for row in rows)
    grand_retention_bytes = sum(row["retention_freed_bytes"] for row in rows)
    max_bytes = max((row["bytes_saved"] for row in rows), default=0)

    log.debug(
        "savings.page.rendered",
        days=days,
        rows=len(rows),
        grand_bytes=grand_bytes,
    )

    return templates.TemplateResponse(
        request,
        "storage_savings.html",
        {
            "title": "Storage savings",
            "active_nav": "stats",
            "rows": rows,
            "days_window": days,
            "grand_bytes": grand_bytes,
            "grand_dedup_hits": grand_dedup_hits,
            "grand_thumb_bytes": grand_thumb_bytes,
            "grand_retention_bytes": grand_retention_bytes,
            "max_bytes": max_bytes,
        },
    )


@router.get("/api/storage-savings.json")
async def storage_savings_json(
    days: int = Query(_DEFAULT_DAYS, ge=1, le=_MAX_DAYS),
) -> JSONResponse:
    """Return the savings timeline as JSON.

    Shape: ``{"days": N, "rows": [{day, bytes_saved, dedup_hits,
    thumb_dedup_bytes, retention_freed_bytes}, ...]}``. ``rows`` is
    oldest first, gaps filled with zeros.
    """
    rows = await chart_data(days=days)
    return JSONResponse({"days": days, "rows": [dict(row) for row in rows]})


__all__ = ["router"]
