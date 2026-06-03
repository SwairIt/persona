"""Admin page + trigger endpoint for the on-disk thumbnail dedup pass.

Pairs with :mod:`app.thumb_dedup`. The GET renders a tiny operator page
with a button; the POST kicks off one batch of
:func:`app.thumb_dedup.scan_and_dedup` and returns the resulting
``{scanned, dedups, bytes_freed}`` tally as JSON so the page can render
it without a full reload.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.thumb_dedup import scan_and_dedup
from app.web.templates_engine import templates

router = APIRouter(tags=["thumb-dedup"])
log = get_logger("persona.thumb_dedup")


@router.get("/admin/thumb-dedup", response_class=HTMLResponse)
async def thumb_dedup_page(request: Request) -> HTMLResponse:
    """Render the operator page with a single trigger button."""
    return templates.TemplateResponse(
        request,
        "thumb_dedup.html",
        {
            "title": "Thumbnail dedup",
            "active_nav": "settings",
        },
    )


@router.post("/admin/thumb-dedup/scan")
async def thumb_dedup_scan() -> JSONResponse:
    """Run one batch of thumbnail dedup and return the tally as JSON."""
    result = await scan_and_dedup()
    log.info(
        "thumb_dedup.route.scan",
        scanned=result["scanned"],
        dedups=result["dedups"],
        bytes_freed=result["bytes_freed"],
    )
    return JSONResponse(dict(result))
