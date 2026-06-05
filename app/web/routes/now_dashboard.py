"""HTTP surface for the /now activity dashboard.

Three endpoints — a full HTML page, a JSON mirror, and an HTMX-friendly
fragment that re-renders only the stats grid every 10 seconds:

* ``GET /now``                 → full page (extends ``base.html``)
* ``GET /api/now.json``        → machine-readable :class:`NowState`
* ``GET /api/now-fragment``    → ``_now_fragment.html`` for HTMX polling

The data model lives in :mod:`app.now_dashboard`; this module is a thin
view layer that does no SQL of its own — keeping the wire shape and the
storage shape in lock-step is the whole point of the split.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.now_dashboard import build_now_state, to_jsonable
from app.web.templates_engine import templates

router = APIRouter(tags=["now-dashboard"])
log = get_logger("persona.web.now_dashboard")


@router.get("/now", response_class=HTMLResponse)
async def now_page(request: Request) -> HTMLResponse:
    """Render the full dashboard page.

    The template wires its own HTMX poller against
    ``/api/now-fragment``; on first render we hand it a complete state
    so the page is meaningful even before the first poll lands.
    """
    state = await build_now_state()
    return templates.TemplateResponse(
        request,
        "now_dashboard.html",
        {
            "title": "Сейчас",
            "active_nav": "timeline",
            "now": state,
        },
    )


@router.get("/api/now.json")
async def now_json() -> JSONResponse:
    """Return the full :class:`NowState` snapshot as JSON."""
    state = await build_now_state()
    return JSONResponse(to_jsonable(state))


@router.get("/api/now-fragment", response_class=HTMLResponse)
async def now_fragment(request: Request) -> HTMLResponse:
    """Return just the stats-grid partial — HTMX polls this every 10s."""
    state = await build_now_state()
    return templates.TemplateResponse(
        request,
        "_now_fragment.html",
        {
            "now": state,
        },
    )


__all__ = ["router"]
