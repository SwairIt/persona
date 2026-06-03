"""HTTP endpoint that returns a cropped-screenshot PNG (v0.85).

The single route here, :func:`screenshot_crop_png`, is a thin wrapper
around :func:`app.screenshot_crop.crop_png` so the share UI can drop
in an ``<img src="/api/screenshot/{id}/crop.png?x=..&y=..&w=..&h=..">``
that yields just the region of interest. The body is a flat PNG —
anything richer would lose its usefulness the moment a user pastes it
into Telegram or Slack.

Responsibilities of the handler:

* **Validate** — translate the helper's structured ``status`` into
  HTTP codes (404 for unknown shots or vanished thumbnails, 400 for a
  bounds typo, 500 only for the truly unexpected).
* **Cache** — the encoded crop is a deterministic function of
  ``(shot_id, x, y, w, h)``, so we set a long ``Cache-Control`` and
  let browsers re-use the bytes instead of hammering Pillow on every
  page render.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from app.logging_setup import get_logger
from app.screenshot_crop import crop_png

router = APIRouter(tags=["screenshot-crop"])
logger = get_logger("persona.crop")


@router.get("/api/screenshot/{screenshot_id}/crop.png")
async def screenshot_crop_png(
    screenshot_id: int,
    x: int = Query(..., ge=0, description="Left edge of crop rectangle, in source pixels."),
    y: int = Query(..., ge=0, description="Top edge of crop rectangle, in source pixels."),
    w: int = Query(..., gt=0, description="Width of crop rectangle, in source pixels."),
    h: int = Query(..., gt=0, description="Height of crop rectangle, in source pixels."),
) -> Response:
    """Return a PNG of the requested screenshot region.

    The handler is a thin wrapper around
    :func:`app.screenshot_crop.crop_png`. We translate its structured
    ``status`` into HTTP responses rather than letting an exception
    bubble — a missing thumbnail is a 404, not a 500, and an
    out-of-bounds rectangle is a 400 with a clear message instead of
    a silently-clamped image the caller did not ask for.

    ``x``/``y``/``w``/``h`` are all required and validated by Pydantic
    at the edge (``ge=0`` / ``gt=0``); the deeper "must lie inside the
    source bitmap" check happens inside :func:`crop_png` so the helper
    stays usable from non-HTTP callers (CLI, tests) with the same
    semantics.
    """
    result = await crop_png(screenshot_id, x, y, w, h)

    status = result["status"]
    if status == "not_found":
        raise HTTPException(status_code=404, detail="Screenshot not found")
    if status == "missing_thumbnail":
        raise HTTPException(status_code=404, detail="Screenshot thumbnail unavailable")
    if status == "bad_bounds":
        raise HTTPException(status_code=400, detail="Crop bounds out of range")
    if status != "ok" or not result["png"]:
        logger.error(
            "crop.unexpected_status",
            shot_id=screenshot_id,
            status=status,
        )
        raise HTTPException(status_code=500, detail="Crop render failed")

    return Response(
        content=result["png"],
        media_type="image/png",
        headers={
            "Content-Length": str(result["size_bytes"]),
            "X-Persona-Crop-Shot": str(screenshot_id),
            "Cache-Control": "public, max-age=86400, immutable",
        },
    )


__all__ = ["router"]
