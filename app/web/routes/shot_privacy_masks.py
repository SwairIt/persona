"""HTTP routes for per-screenshot privacy mask regions.

Five endpoints:

* ``GET /screenshot/{shot_id}/privacy-mask`` — HTML editor page. Renders
  the canvas with the original thumbnail as the background and lets the
  user paint black rectangles by mouse-drag. Existing masks are listed
  below the canvas with a per-row Delete button.
* ``POST /api/screenshot/{shot_id}/privacy-mask`` — JSON body
  ``{"x", "y", "width", "height", "label"?}`` inserts a new mask and
  returns the created row.
* ``DELETE /api/screenshot/{shot_id}/privacy-mask/{mask_id}`` — removes
  a single mask by id.
* ``GET /api/screenshot/{shot_id}/privacy-mask/render.png`` — returns
  the safe-to-share PNG with every mask painted in solid black. ``404``
  when there are no masks or the thumbnail is gone.
* ``GET /api/screenshot/{shot_id}/privacy-mask.json`` — returns the list
  of stored masks for the shot.

The HTML page extends ``base.html`` with ``title="Privacy mask"`` and
``active_nav="timeline"`` (the editor sits inside the screenshot-feed
surface area, not under stats).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from app.logging_setup import get_logger
from app.shot_privacy_masks import (
    add_mask,
    list_masks_for_shot,
    remove_mask,
    render_masked_image,
)
from app.storage.db import get_connection
from app.web.routes.thumbnails import thumbnail_url
from app.web.templates_engine import templates

log = get_logger("persona.shot_privacy_masks.routes")

router = APIRouter(tags=["shot_privacy_masks"])


def _coerce_int(payload: dict[str, Any], key: str) -> int:
    """Pull a required int field out of a JSON dict or raise ``HTTPException``.

    Centralised so the POST handler stays small and we surface a
    consistent ``400`` rather than leaking ``KeyError`` / ``TypeError``
    out of the route.
    """
    if key not in payload:
        raise HTTPException(
            status_code=400, detail=f"body is missing '{key}'"
        )
    value = payload[key]
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail=f"'{key}' must be an integer"
        ) from exc


def _coerce_optional_label(payload: dict[str, Any]) -> str | None:
    """Extract an optional ``label`` string from a JSON dict, or ``None``."""
    raw = payload.get("label")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="'label' must be a string")
    stripped = raw.strip()
    return stripped or None


async def _load_shot_or_404(shot_id: int) -> dict[str, Any]:
    """Return a shot dict (``id``, ``thumbnail_path``, ``window_title``, ``app_name``)
    or raise ``404`` when the row is missing."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, thumbnail_path, window_title, app_name, captured_at "
            "FROM screenshots WHERE id = ?",
            (int(shot_id),),
        )
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")

    return {
        "id": int(row[0]),
        "thumbnail_path": (str(row[1]) if row[1] is not None else None),
        "window_title": (str(row[2]) if row[2] is not None else None),
        "app_name": (str(row[3]) if row[3] is not None else None),
        "captured_at": (str(row[4]) if row[4] is not None else None),
    }


@router.get(
    "/screenshot/{shot_id}/privacy-mask",
    response_class=HTMLResponse,
)
async def privacy_mask_editor_page(
    shot_id: int, request: Request
) -> HTMLResponse:
    """Render the canvas-based privacy mask editor for ``shot_id``."""
    shot = await _load_shot_or_404(shot_id)
    masks = await list_masks_for_shot(shot_id)
    thumb_url = thumbnail_url(shot["thumbnail_path"])

    return templates.TemplateResponse(
        request,
        "shot_privacy_mask_editor.html",
        {
            "title": "Privacy mask",
            "active_nav": "timeline",
            "shot": shot,
            "masks": masks,
            "thumb_url": thumb_url,
        },
    )


@router.post(
    "/api/screenshot/{shot_id}/privacy-mask",
    response_class=JSONResponse,
)
async def create_privacy_mask(
    shot_id: int, request: Request
) -> JSONResponse:
    """Add one privacy-mask rectangle and return its persisted row."""
    try:
        payload = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    # Fast-fail if the shot itself is missing — avoids creating a
    # rectangle that immediately CASCADEs to nothing.
    await _load_shot_or_404(shot_id)

    x = _coerce_int(payload, "x")
    y = _coerce_int(payload, "y")
    width = _coerce_int(payload, "width")
    height = _coerce_int(payload, "height")
    label = _coerce_optional_label(payload)

    mask_id = await add_mask(
        shot_id=shot_id,
        x=x,
        y=y,
        width=width,
        height=height,
        label=label,
    )

    # Re-read the row so the client receives the canonical (clamped)
    # values plus ``created_at`` without having to compose them.
    rows = await list_masks_for_shot(shot_id)
    created = next((m for m in rows if m["id"] == mask_id), None)
    if created is None:
        # Should never happen — the row was just inserted in the same
        # connection helper. Defensive 500 keeps mypy happy.
        raise HTTPException(
            status_code=500, detail="mask vanished after insert"
        )

    return JSONResponse(created, status_code=201)


@router.delete(
    "/api/screenshot/{shot_id}/privacy-mask/{mask_id}",
    response_class=JSONResponse,
)
async def delete_privacy_mask(
    shot_id: int, mask_id: int
) -> JSONResponse:
    """Remove one privacy-mask rectangle by id."""
    # Validating the shot first means a missing shot returns 404
    # rather than silently no-op'ing the DELETE. The mask_id itself is
    # tolerated as "already gone" inside remove_mask.
    await _load_shot_or_404(shot_id)
    await remove_mask(mask_id)
    return JSONResponse({"deleted": int(mask_id), "shot_id": int(shot_id)})


@router.get(
    "/api/screenshot/{shot_id}/privacy-mask/render.png",
    response_class=Response,
)
async def render_privacy_mask_png(shot_id: int) -> Response:
    """Return the safe-to-share PNG with every mask painted in solid black."""
    # Confirm the shot exists before touching Pillow so a fat-fingered
    # URL gets a clean 404 instead of an empty-bytes 404 from the
    # renderer.
    await _load_shot_or_404(shot_id)

    png_bytes = await render_masked_image(shot_id)
    if png_bytes is None:
        raise HTTPException(
            status_code=404,
            detail="No masks to render (or source thumbnail missing)",
        )

    filename = f"persona-shot-{shot_id}-masked.png"
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Content-Length": str(len(png_bytes)),
            # No public caching — the rectangles can be edited at any
            # moment and the redacted variant must stay fresh.
            "Cache-Control": "private, no-store",
        },
    )


@router.get(
    "/api/screenshot/{shot_id}/privacy-mask.json",
    response_class=JSONResponse,
)
async def list_privacy_masks_json(shot_id: int) -> JSONResponse:
    """Return every privacy mask attached to ``shot_id`` as JSON."""
    # Don't 404 here when the shot is missing — the operator may be
    # polling the list right after a CASCADE delete; an empty list is
    # the honest answer.
    items = await list_masks_for_shot(shot_id)
    return JSONResponse(items)


__all__ = ["router"]
