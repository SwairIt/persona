"""Admin page + trigger endpoint for the thumbnail regen sweep.

Pairs with :mod:`app.thumb_regen`. The GET renders a small operator
page with a single trigger button; the POST runs one batch of
:func:`app.thumb_regen.regen_missing` and returns the resulting
``{scanned, regenerated, failed}`` tally as JSON so the page can
render it without a full reload.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.thumb_regen import regen_missing
from app.web.templates_engine import templates

router = APIRouter(tags=["thumb-regen"])
log = get_logger("persona.thumb_regen")


@router.get("/admin/thumb-regen", response_class=HTMLResponse)
async def thumb_regen_page(request: Request) -> HTMLResponse:
    """Render the operator page with a single trigger button."""
    return templates.TemplateResponse(
        request,
        "thumb_regen.html",
        {
            "title": "Thumbnail regen",
            "active_nav": "settings",
        },
    )


@router.post("/admin/thumb-regen/run")
async def thumb_regen_run() -> JSONResponse:
    """Run one batch of thumbnail regen and return the tally as JSON."""
    result = await regen_missing()
    log.info(
        "thumb_regen.route.run",
        scanned=result["scanned"],
        regenerated=result["regenerated"],
        failed=result["failed"],
    )
    return JSONResponse(dict(result))
