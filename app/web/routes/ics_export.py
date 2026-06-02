"""HTTP route for the iCalendar (.ics) activity export.

``GET /export/calendar.ics?days=90`` returns a ``text/calendar`` document
that contains one all-day VEVENT per day in the lookback window where
Persona captured at least one screenshot.  Users can subscribe to or
import the file in Google Calendar / Apple Calendar / Outlook to get
retrospective context next to their meetings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.ics_export import export_ics
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger("persona.ics")

router = APIRouter(prefix="/export", tags=["ics-export"])

# Max window mirrors the clamp inside :func:`app.ics_export.export_ics`.
_MAX_DAYS = 3650
_MIN_DAYS = 1


@router.get("/calendar.ics", response_model=None)
async def export_calendar_ics(
    days: int = Query(default=90, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> StreamingResponse:
    """Stream an iCalendar document covering the last ``days`` days."""
    try:
        body = await export_ics(days_back=days)
    except Exception:
        log.exception("ics.route.failed", days=days)
        raise HTTPException(status_code=500, detail="ICS export failed") from None

    payload = body.encode("utf-8")
    filename = f"persona-{days}d.ics"

    def _iter() -> Iterator[bytes]:
        yield payload

    log.info("ics.route.ok", days=days, bytes=len(payload))

    return StreamingResponse(
        _iter(),
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
    )


__all__ = ["router"]
