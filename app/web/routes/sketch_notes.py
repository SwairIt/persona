"""HTTP surfaces for the freehand sketch-notes feature (v1.48).

Six routes:

    GET    /sketch                       — drawing canvas (editor)
    GET    /sketches                     — list of all sketches with
                                            thumbnails
    GET    /sketch/{id}                  — full-size view of one sketch
    POST   /api/sketches                 — create a new sketch from a
                                            JSON ``{title, svg_payload,
                                            width, height, tags}`` body
    DELETE /api/sketches/{id}            — drop one sketch
    GET    /api/sketches.json            — JSON list snapshot
    GET    /api/sketch/{id}/render.svg   — raw ``image/svg+xml`` for
                                            embedding in ``<img>`` tags

The HTML pages render through the shared :mod:`app.web.templates_engine`
templates, matching the look and feel of every other v1.4x memory
surface. The JSON endpoints return ``{ok, id}`` so the editor can call
them via ``fetch`` and update its UI without a full page reload.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app.logging_setup import get_logger
from app.sketch_notes import (
    create_sketch,
    delete_sketch,
    get_sketch,
    list_sketches,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["sketch-notes"])

log = get_logger("persona.web.sketch_notes")

#: Page-size for the list view. Sketches are tiny on disk but every
#: thumbnail still costs a DOM node; 60 is plenty for visual scanning
#: without dragging the page to a crawl on a low-end laptop.
_LIST_PAGE_SIZE: int = 60

#: Hard floor / ceiling for the sketch viewport. Anything outside is
#: a UI bug we want to surface as 400 rather than silently clamp.
_DIMENSION_MIN: int = 1
_DIMENSION_MAX: int = 8192

#: Hard cap on the SVG payload size. A typical doodle weighs a few
#: kilobytes; ten megabytes is two orders of magnitude beyond any
#: reasonable hand-drawn sketch and almost certainly a bug or attack.
_PAYLOAD_MAX_BYTES: int = 10 * 1024 * 1024


class _CreatePayload(BaseModel):
    """JSON body for ``POST /api/sketches``."""

    title: str | None = Field(default=None, max_length=512)
    svg_payload: str = Field(..., min_length=1)
    width: int = Field(..., ge=_DIMENSION_MIN, le=_DIMENSION_MAX)
    height: int = Field(..., ge=_DIMENSION_MIN, le=_DIMENSION_MAX)
    tags: str | None = Field(default=None, max_length=512)


def _metadata_only(item: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``item`` with the bulky ``svg_payload`` removed.

    The list view's API endpoint and the list template only need the
    metadata to lay out cards — the full document is fetched separately
    via the per-sketch render endpoint when the user clicks through.
    """
    trimmed = dict(item)
    trimmed.pop("svg_payload", None)
    return trimmed


@router.get("/sketch", response_class=HTMLResponse)
async def sketch_editor_page(request: Request) -> HTMLResponse:
    """Render the freehand drawing canvas + save form."""
    return templates.TemplateResponse(
        request,
        "sketch_editor.html",
        {
            "title": "Sketch",
            "active_nav": "memory",
        },
    )


@router.get("/sketches", response_class=HTMLResponse)
async def sketch_list_page(request: Request) -> HTMLResponse:
    """Render the list of every sketch with thumbnail previews."""
    items = await list_sketches(limit=_LIST_PAGE_SIZE)
    log.info("sketch_notes.list_page", count=len(items))
    return templates.TemplateResponse(
        request,
        "sketch_list.html",
        {
            "title": "Sketches",
            "active_nav": "memory",
            "items": items,
        },
    )


@router.get("/sketch/{sketch_id}", response_class=HTMLResponse)
async def sketch_detail_page(
    request: Request, sketch_id: int
) -> HTMLResponse:
    """Render the full-size view of one sketch.

    404s when the row is missing so a stale bookmark does not silently
    render a blank page.
    """
    sketch = await get_sketch(sketch_id)
    if sketch is None:
        raise HTTPException(status_code=404, detail="Sketch not found")
    return templates.TemplateResponse(
        request,
        "sketch_detail.html",
        {
            "title": sketch["title"] or f"Sketch #{sketch_id}",
            "active_nav": "memory",
            "sketch": sketch,
        },
    )


@router.post("/api/sketches")
async def sketch_create(payload: _CreatePayload) -> JSONResponse:
    """Persist a new sketch and return its assigned id.

    The body is validated by :class:`_CreatePayload`; we additionally
    enforce the byte-size cap here because pydantic's ``max_length`` is
    a character count and we care about bytes-on-the-wire.
    """
    if len(payload.svg_payload.encode("utf-8")) > _PAYLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail="svg_payload too large")
    new_id = await create_sketch(
        title=payload.title,
        svg_payload=payload.svg_payload,
        width=payload.width,
        height=payload.height,
        tags=payload.tags,
    )
    return JSONResponse({"ok": True, "id": new_id}, status_code=201)


@router.delete("/api/sketches/{sketch_id}")
async def sketch_delete(sketch_id: int) -> JSONResponse:
    """Drop one sketch by id. Idempotent — never 404s."""
    await delete_sketch(sketch_id)
    return JSONResponse({"ok": True, "id": sketch_id})


@router.get("/api/sketches.json", response_class=JSONResponse)
async def sketch_list_json() -> JSONResponse:
    """JSON snapshot of every sketch (metadata only, no payloads)."""
    items = await list_sketches(limit=_LIST_PAGE_SIZE)
    return JSONResponse(
        {"ok": True, "items": [_metadata_only(item) for item in items]}
    )


@router.get("/api/sketch/{sketch_id}/render.svg")
async def sketch_render_svg(sketch_id: int) -> Response:
    """Return the raw SVG document for embedding in ``<img>`` tags.

    Served as ``image/svg+xml`` so the browser renders the markup
    natively. Because the payload was sanitised at insert time
    (see :func:`app.sketch_notes.sanitize_svg`), no further escaping is
    required here.
    """
    sketch = await get_sketch(sketch_id)
    if sketch is None:
        raise HTTPException(status_code=404, detail="Sketch not found")
    return Response(
        content=sketch["svg_payload"],
        media_type="image/svg+xml",
        headers={"Cache-Control": "private, max-age=60"},
    )


__all__ = ["router"]
