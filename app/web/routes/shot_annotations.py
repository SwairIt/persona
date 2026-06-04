"""HTTP surface for per-screenshot visual annotations (v1.20).

Three JSON endpoints plus one HTML editor page:

- ``GET    /api/screenshot/{shot_id}/annotation.json`` — read or 404.
- ``POST   /api/screenshot/{shot_id}/annotation``     — upsert; rejects
  payloads larger than :data:`app.shot_annotations.MAX_PAYLOAD_BYTES`
  with ``413 Payload Too Large``.
- ``DELETE /api/screenshot/{shot_id}/annotation``     — 204 if a row
  existed, 404 otherwise.
- ``GET    /shot/{shot_id}/annotate``                 — HTML editor
  that renders the thumbnail with an SVG canvas overlay and a tiny
  inline JS toolbox (rectangle / arrow / text / undo / save).

The module follows the project's "one router, registered elsewhere"
convention — ``app/web/main.py`` is intentionally NOT edited here.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from app.logging_setup import get_logger
from app.shot_annotations import (
    MAX_PAYLOAD_BYTES,
    delete_annotation,
    get_annotation,
    sanitise_svg,
    upsert_annotation,
)
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.templates_engine import templates

router = APIRouter(tags=["shot-annotations"])
log = get_logger("persona.web.shot_annotations")


class _AnnotationPayload(BaseModel):
    """POST body for the upsert endpoint."""

    svg_payload: str = Field(..., max_length=MAX_PAYLOAD_BYTES * 4)
    # ``max_length`` is in characters, not bytes; a generous 4x cap
    # rejects obviously-huge bodies early without forbidding multi-byte
    # UTF-8. The real byte-precise check lives in ``upsert_annotation``.


async def _require_screenshot(shot_id: int) -> None:
    """Raise 404 if ``shot_id`` does not exist."""
    async with get_connection() as conn:
        shot = await get_screenshot(conn, shot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")


@router.get("/api/screenshot/{shot_id}/annotation.json")
async def annotation_read(shot_id: int) -> JSONResponse:
    """Return the stored annotation row or 404."""
    await _require_screenshot(shot_id)
    row = await get_annotation(shot_id)
    if row is None:
        raise HTTPException(status_code=404, detail="No annotation")
    return JSONResponse(row)


@router.post("/api/screenshot/{shot_id}/annotation")
async def annotation_upsert(
    shot_id: int,
    payload: _AnnotationPayload,
) -> JSONResponse:
    """Upsert the annotation. Returns the persisted row."""
    await _require_screenshot(shot_id)
    try:
        row = await upsert_annotation(shot_id, payload.svg_payload)
    except ValueError as exc:
        log.warning(
            "shot_annotations.too_large",
            shot_id=shot_id,
            error=str(exc),
        )
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return JSONResponse(
        {
            "id": row["id"],
            "screenshot_id": row["screenshot_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
    )


@router.delete("/api/screenshot/{shot_id}/annotation")
async def annotation_delete(shot_id: int) -> Response:
    """204 when a row was removed, 404 when there was nothing to delete."""
    await _require_screenshot(shot_id)
    removed = await delete_annotation(shot_id)
    if not removed:
        raise HTTPException(status_code=404, detail="No annotation")
    return Response(status_code=204)


@router.get("/shot/{shot_id}/annotate", response_class=HTMLResponse)
async def annotation_editor(request: Request, shot_id: int) -> HTMLResponse:
    """Render the inline editor (toolbox + SVG canvas overlay)."""
    async with get_connection() as conn:
        shot = await get_screenshot(conn, shot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    existing = await get_annotation(shot_id)
    initial_svg = sanitise_svg(existing["svg_payload"]) if existing else ""
    updated_at = existing["updated_at"] if existing else None

    return templates.TemplateResponse(
        request,
        "shot_annotate.html",
        {
            "title": "Аннотации",
            "active_nav": "timeline",
            "shot": shot,
            "initial_svg": initial_svg,
            "updated_at": updated_at,
            "max_payload_bytes": MAX_PAYLOAD_BYTES,
        },
    )
