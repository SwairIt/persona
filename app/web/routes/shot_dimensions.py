"""Admin page + trigger endpoint for the screenshot-dimensions backfill.

Pairs with :mod:`app.shot_dimensions`. The GET renders a tiny operator
page with a button; the POST kicks off one batch of
:func:`app.shot_dimensions.backfill_missing` and returns the resulting
``{scanned, updated, skipped}`` tally as JSON so the page can render it
without a full reload.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.shot_dimensions import backfill_missing
from app.web.templates_engine import templates

router = APIRouter(tags=["shot-dimensions"])
log = get_logger("persona.dimensions")


@router.get("/admin/shot-dimensions", response_class=HTMLResponse)
async def shot_dimensions_page(request: Request) -> HTMLResponse:
    """Render the operator page with a single trigger button."""
    return templates.TemplateResponse(
        request,
        "shot_dimensions.html",
        {
            "title": "Shot dimensions backfill",
            "active_nav": "settings",
        },
    )


@router.post("/admin/shot-dimensions/backfill")
async def shot_dimensions_backfill() -> JSONResponse:
    """Run one batch of dimension backfill and return the tally as JSON."""
    result = await backfill_missing()
    log.info(
        "dimensions.route.backfill",
        scanned=result["scanned"],
        updated=result["updated"],
        skipped=result["skipped"],
    )
    return JSONResponse(dict(result))
