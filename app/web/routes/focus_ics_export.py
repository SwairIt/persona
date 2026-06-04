"""HTTP routes for the focus-session iCalendar (.ics) export.

Two endpoints:

* ``GET /focus/calendar.ics?days=90`` — downloadable ``text/calendar``
  document covering every completed focus session in the lookback
  window. Mirrors the daily-rollup endpoint at
  ``GET /export/calendar.ics`` but emits *timed* events (one VEVENT per
  session) instead of all-day rollups.
* ``GET /focus/calendar`` — small HTML page that lets the operator pick
  a window length (30 / 60 / 90 / 365 days) and click through to the
  ``.ics`` URL. Extends ``base.html`` so the global nav stays visible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from app.focus_ics import build_focus_ics
from app.logging_setup import get_logger
from app.web.templates_engine import templates

if TYPE_CHECKING:
    from collections.abc import Iterator

log = get_logger("persona.focus_ics")

router = APIRouter(tags=["focus-ics-export"])

# Mirrors the clamp inside :func:`app.focus_ics.build_focus_ics` so the
# query parser rejects out-of-range values before the export runs.
_MIN_DAYS = 1
_MAX_DAYS = 3650
# Window options the picker page renders as buttons. Kept here (instead
# of hard-coded in the template) so the list stays a single source of
# truth — adding ``180`` is a one-line edit and the page updates.
_WINDOW_OPTIONS: tuple[int, ...] = (30, 60, 90, 365)


@router.get("/focus/calendar.ics", response_model=None)
async def focus_calendar_ics(
    days: int = Query(default=90, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> StreamingResponse:
    """Stream the focus-session iCalendar document."""
    try:
        body = await build_focus_ics(days=days)
    except Exception:
        log.exception("focus_ics.route.failed", days=days)
        raise HTTPException(
            status_code=500,
            detail="Focus ICS export failed",
        ) from None

    payload = body.encode("utf-8")
    filename = "persona-focus.ics"

    def _iter() -> Iterator[bytes]:
        yield payload

    log.info("focus_ics.route.ok", days=days, bytes=len(payload))

    return StreamingResponse(
        _iter(),
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(payload)),
            "Cache-Control": "no-store",
        },
    )


@router.get("/focus/calendar", response_class=HTMLResponse)
async def focus_calendar_page(request: Request) -> HTMLResponse:
    """Render the download-picker page."""
    log.info("focus_ics.page", options=list(_WINDOW_OPTIONS))
    return templates.TemplateResponse(
        request,
        "focus_calendar_export.html",
        {
            "title": "Calendar export",
            "active_nav": "focus",
            "window_options": _WINDOW_OPTIONS,
            "default_days": 90,
        },
    )


__all__ = ["router"]
