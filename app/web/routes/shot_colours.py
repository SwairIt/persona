"""HTTP surface for the per-shot colour palette.

v1.8 feature 2/3. Two endpoints share the same data source
(:func:`app.shot_colours.compute_palette`):

* ``GET /api/screenshot/{id}/colours.json`` — JSON payload consumed by
  third-party tools and by the in-page detail view when it wants to
  refresh the palette without a full reload.
* ``GET /screenshot/{id}/colours`` — Tailwind-styled HTML page that
  renders a horizontal colour bar with one segment per palette entry,
  weighted by ``weight_pct``.

Both endpoints intentionally trigger the cache: hitting the page once
is enough to back-fill ``shot_colour`` for that screenshot — there is
no admin-only "compute palette" button, because the OCR worker already
fires the same call as a side-channel after every successful OCR.

A missing screenshot returns ``404``; a screenshot whose thumbnail
cannot be quantized (missing file, PIL refused the bytes) returns an
empty palette ``[]`` rather than ``404`` — the row exists, it just has
no extractable colour signal yet.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.shot_colours import PaletteEntry, compute_palette
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.templates_engine import templates

router = APIRouter(tags=["shot-colours"])
log = get_logger("persona.shot_colours")


async def _resolve_palette(screenshot_id: int) -> list[PaletteEntry]:
    """Return the palette for ``screenshot_id`` or ``[]`` when none could be derived.

    Wrapping :func:`compute_palette`'s ``None`` return in ``[]`` here
    means the JSON endpoint always emits a stable shape
    (``{"palette": [...]}``) — clients never need to special-case a
    ``null`` payload — and the HTML page can iterate the (possibly
    empty) list without a ``{% if palette %}`` guard around the bar
    itself.
    """
    palette = await compute_palette(screenshot_id)
    if palette is None:
        return []
    return list(palette)


@router.get("/api/screenshot/{screenshot_id}/colours.json", response_class=JSONResponse)
async def shot_colours_json(screenshot_id: int) -> JSONResponse:
    """Return the per-shot palette as JSON.

    Always returns ``200`` for an existing screenshot, even when the
    palette could not be derived — the empty-list body is the documented
    "no signal yet" response.
    """
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    palette = await _resolve_palette(screenshot_id)
    log.info(
        "shot_colours.route.json",
        screenshot_id=screenshot_id,
        entries=len(palette),
    )
    return JSONResponse({"screenshot_id": screenshot_id, "palette": palette})


@router.get("/screenshot/{screenshot_id}/colours", response_class=HTMLResponse)
async def shot_colours_page(request: Request, screenshot_id: int) -> HTMLResponse:
    """Render the Tailwind page with the horizontal colour bar."""
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    palette = await _resolve_palette(screenshot_id)
    log.info(
        "shot_colours.route.page",
        screenshot_id=screenshot_id,
        entries=len(palette),
    )
    return templates.TemplateResponse(
        request,
        "shot_colours.html",
        {
            "title": f"Colours · Screenshot #{screenshot_id}",
            "active_nav": "timeline",
            "shot": shot,
            "palette": palette,
        },
    )
