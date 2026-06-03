"""HTTP endpoint that streams a framed-screenshot PNG (v0.72).

The single route here, :func:`screenshot_frame_png`, wraps a stored
screenshot in a stylised window-chrome border so the admin's share
panel can drop in a one-click "Framed image" download link. The asset
is intentionally a flat PNG — anything richer (SVG, HTML preview)
would lose its social-share utility the moment a user pastes it into
Telegram or Slack.

Responsibilities of the handler:

* **Validate** — translate the renderer's structured ``status`` into
  HTTP codes (404 for unknown shots, 400 for bad style, 500 only for
  the truly unexpected).
* **Cache the file path** — the PNG is a deterministic function of
  ``(shot_id, style)``, so we write it into a stable temp location and
  let repeat requests re-render in place. ``optimize=True`` is set on
  the encoder side, so the cost is roughly bounded by Pillow's PNG
  encoder.
* **Stream** — read the PNG back in 64 KiB chunks rather than slurping
  it into memory. Mirrors :mod:`app.web.routes.digest_card` so the two
  share-PNG routes behave identically on small VMs.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.logging_setup import get_logger
from app.screenshot_frame import FrameStyle, build_framed_png

if TYPE_CHECKING:
    from collections.abc import Iterator

router = APIRouter(tags=["screenshot-frame"])
logger = get_logger("persona.frame")

# 64 KiB matches the existing share-card route — sweet spot between
# syscall overhead and per-chunk memory pressure on small VMs.
_PNG_CHUNK_BYTES = 64 * 1024

_SUPPORTED_STYLES: frozenset[str] = frozenset({"mac"})

# Hard cap on the per-request watermark length so a runaway query string
# can't OOM the renderer or balloon the output PNG. The renderer also
# clips internally; this is just the cheaper gate at the HTTP edge.
_WATERMARK_QUERY_MAX = 120


def _frame_path(shot_id: int, style: str, *, watermarked: bool) -> Path:
    """Return the on-disk path Persona writes a given frame to.

    Keeps the temp tree predictable so repeat hits reuse a single file
    per ``(shot_id, style, watermarked?)``. We keep watermarked and
    non-watermarked outputs in distinct files instead of hashing the
    text — the cache stays trivial to reason about, and on a busy
    operator panel the two-file footprint is negligible.
    """
    tmp_dir = Path(tempfile.gettempdir()) / "persona-screenshot-frame"
    suffix = "wm" if watermarked else "plain"
    return tmp_dir / f"persona-frame-{shot_id}-{style}-{suffix}.png"


@router.get("/api/screenshot/{screenshot_id}/frame.png", response_model=None)
async def screenshot_frame_png(
    screenshot_id: int,
    style: str = Query(default="mac", description="Frame chrome style. Only 'mac' for v0.72."),
    watermark: str | None = Query(
        default=None,
        max_length=_WATERMARK_QUERY_MAX,
        description=(
            "Optional watermark text drawn on the bottom-right of the framed image. "
            "Omit to fall back on the operator-wide kv_settings.framed_watermark "
            "default; pass an empty string to force no watermark."
        ),
    ),
) -> StreamingResponse:
    """Stream a framed-screenshot PNG.

    The handler is a thin wrapper around
    :func:`app.screenshot_frame.build_framed_png`. We translate its
    structured ``status`` into HTTP responses rather than letting an
    exception bubble — a missing thumbnail is a 404, not a 500, and
    a tomorrow-added style typo on the caller side deserves a 400.
    """
    if style not in _SUPPORTED_STYLES:
        logger.warning(
            "frame.route_bad_style",
            shot_id=screenshot_id,
            style=style,
        )
        raise HTTPException(status_code=400, detail="Unsupported frame style")

    # Cast is safe: ``style`` was just validated against the literal set.
    typed_style: FrameStyle = "mac"

    # When the caller did not pass ?watermark=… at all we leave the
    # value at ``None`` so the renderer can consult its kv default; an
    # empty string ("?watermark=") forces "no watermark" through. Either
    # way the cache file path only branches on "any watermark drawn?",
    # which we don't know until the renderer returns — pre-compute the
    # most likely answer here so repeat hits hit the same file.
    watermark_will_render = watermark is None or bool(watermark.strip())
    out_path = _frame_path(
        screenshot_id,
        typed_style,
        watermarked=watermark_will_render,
    )
    result = await build_framed_png(
        screenshot_id,
        out_path,
        style=typed_style,
        watermark=watermark,
    )

    status = result["status"]
    if status == "not_found":
        raise HTTPException(status_code=404, detail="Screenshot not found")
    if status == "missing_thumbnail":
        raise HTTPException(status_code=404, detail="Screenshot thumbnail unavailable")
    if status == "bad_style":  # pragma: no cover - guarded above
        raise HTTPException(status_code=400, detail="Unsupported frame style")
    if status != "ok" or result["path"] is None:
        logger.error(
            "frame.unexpected_status",
            shot_id=screenshot_id,
            status=status,
        )
        raise HTTPException(status_code=500, detail="Frame render failed")

    png_path = Path(result["path"])
    size_bytes = result["size_bytes"]

    def _iter_file() -> Iterator[bytes]:
        with png_path.open("rb") as fh:
            while True:
                chunk = fh.read(_PNG_CHUNK_BYTES)
                if not chunk:
                    break
                yield chunk

    filename = f"persona-frame-{screenshot_id}.png"
    return StreamingResponse(
        _iter_file(),
        media_type="image/png",
        headers={
            "Content-Disposition": f'inline; filename="{filename}"',
            "Content-Length": str(size_bytes),
            "X-Persona-Frame-Style": typed_style,
            "X-Persona-Frame-Shot": str(screenshot_id),
            "Cache-Control": "public, max-age=3600",
        },
    )


__all__ = ["router"]
