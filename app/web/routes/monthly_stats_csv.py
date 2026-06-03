"""HTTP route for the monthly-rollup stats CSV export.

``GET /export/monthly-stats.csv?months=12`` streams a ``text/csv``
document with one row per ``(year-month, app_name)`` cell over the
last ``months`` months (current month plus ``months - 1`` preceding
ones).  See :func:`app.monthly_stats_csv.export_monthly_stats_csv`
for the column contract.

Single-chunk streaming via :class:`fastapi.responses.StreamingResponse`
mirrors :mod:`app.web.routes.stats_csv` — keeps the
``Content-Disposition`` filename header authoritative and forces a
download rather than an inline render regardless of the inferred media
type.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.logging_setup import get_logger
from app.monthly_stats_csv import export_monthly_stats_csv

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger("persona.monthly_stats_csv")

router = APIRouter(prefix="/export", tags=["monthly-stats-csv"])

# Mirrors the clamp inside
# :func:`app.monthly_stats_csv.export_monthly_stats_csv`.
_MIN_MONTHS = 1
_MAX_MONTHS = 120


@router.get("/monthly-stats.csv", response_model=None)
async def export_monthly_stats_csv_route(
    months: int = Query(default=12, ge=_MIN_MONTHS, le=_MAX_MONTHS),
) -> StreamingResponse:
    """Stream the monthly-rollup stats CSV for the last ``months`` months."""
    try:
        body = await export_monthly_stats_csv(months_back=months)
    except Exception:
        log.exception("monthly_stats_csv.route.failed", months=months)
        raise HTTPException(
            status_code=500,
            detail="monthly stats CSV export failed",
        ) from None

    payload = body.encode("utf-8")
    filename = f"persona-monthly-stats-{months}m.csv"

    def _iter() -> Iterator[bytes]:
        yield payload

    log.info("monthly_stats_csv.route.ok", months=months, bytes=len(payload))

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
