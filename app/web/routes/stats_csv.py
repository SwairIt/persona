"""HTTP route for the comprehensive per-day-per-app stats CSV export.

``GET /export/stats.csv?days=90`` streams a ``text/csv`` document with
one row per ``(date, app_name)`` cell over the last ``days`` days.  See
:func:`app.stats_csv.export_stats_csv` for the column contract.

Streaming via :class:`fastapi.responses.StreamingResponse` (single
chunk) mirrors :mod:`app.web.routes.ics_export` — it keeps the
``Content-Disposition`` filename header authoritative and lets browsers
download-rather-than-render the payload regardless of the inferred
media type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.logging_setup import get_logger
from app.stats_csv import export_stats_csv

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger("persona.stats_csv")

router = APIRouter(prefix="/export", tags=["stats-csv"])

# Mirrors the clamp inside :func:`app.stats_csv.export_stats_csv`.
_MIN_DAYS = 1
_MAX_DAYS = 3650


@router.get("/stats.csv", response_model=None)
async def export_stats_csv_route(
    days: int = Query(default=90, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> StreamingResponse:
    """Stream the comprehensive stats CSV for the last ``days`` days."""
    try:
        body = await export_stats_csv(days_back=days)
    except Exception:
        log.exception("stats_csv.route.failed", days=days)
        raise HTTPException(status_code=500, detail="stats CSV export failed") from None

    payload = body.encode("utf-8")
    filename = f"persona-stats-{days}d.csv"

    def _iter() -> Iterator[bytes]:
        yield payload

    log.info("stats_csv.route.ok", days=days, bytes=len(payload))

    return StreamingResponse(
        _iter(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router"]
