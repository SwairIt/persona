"""Screenshot crop export (v0.85).

Persona's screenshot detail view already lets an operator inspect a
captured frame in full, but every downstream sharing flow — the framed
PNG, the share-link card, the digest collage — wants a *part* of the
shot, not the whole bitmap. Until v0.85 the only way to get one was to
download the original and crop it manually in an editor; that breaks
on a tablet, leaks the full frame on a screenshare, and adds an extra
copy of the raw pixels to whoever performs the crop.

This module is the missing piece: a single, side-effect-free helper
that opens the stored screenshot, validates the requested rectangle,
and returns the cropped region as raw PNG bytes. The output is a pure
function of ``(shot_id, x, y, w, h)`` so the route layer can hand the
bytes straight back to the browser with a long ``Cache-Control``
without worrying about staleness.

The public entry point :func:`crop_png` is **async on purpose**: it
loads the source bitmap from disk and pushes every synchronous Pillow
call (open / crop / save) onto a worker thread via
:func:`anyio.to_thread.run_sync`, so a slow PNG encode never stalls
uvicorn's event loop.

Design rules baked in:

* **Tolerant**, like :mod:`app.screenshot_frame`. A missing
  screenshot, a thumbnail file that vanished between capture and
  request, an out-of-bounds rectangle — none of these raise. We
  surface a structured ``status`` so the route layer can translate it
  to an HTTP code instead of crashing.
* **Strict bounds**. The crop rectangle must lie entirely inside the
  source bitmap with strictly positive width and height. We do not
  clamp silently: a partially-inside box is rejected the same way an
  entirely-outside one is, so an operator typo cannot quietly produce
  a smaller image than they asked for.
* **Pure-function output**. Given the same shot id and rectangle, the
  bytes are stable — callers can safely treat the result as a
  cacheable asset.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TypedDict

import anyio
from PIL import Image

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot

log = get_logger("persona.crop")


class CropResult(TypedDict):
    """Return payload for :func:`crop_png`.

    ``status`` values:

    * ``"ok"`` — bytes were produced; ``png`` is a complete PNG body
      and ``size_bytes`` is its length.
    * ``"not_found"`` — no such screenshot row.
    * ``"missing_thumbnail"`` — row exists but its source file is gone
      or unreadable.
    * ``"bad_bounds"`` — the requested rectangle does not lie strictly
      inside the source bitmap, or has non-positive width/height.
    """

    status: str
    png: bytes
    size_bytes: int


def _validate_bounds(
    *,
    x: int,
    y: int,
    w: int,
    h: int,
    src_width: int,
    src_height: int,
) -> bool:
    """Return ``True`` iff the crop rectangle fits the source bitmap.

    The check is deliberately strict: both width and height must be
    positive, the top-left corner must be non-negative, and the
    bottom-right corner must lie within the source extents. Anything
    else is a 400 at the route layer — silently clamping a typo would
    return a different image than the caller asked for, and that's a
    sharper-edged bug than a clear rejection.
    """
    if w <= 0 or h <= 0:
        return False
    if x < 0 or y < 0:
        return False
    if x + w > src_width:
        return False
    return y + h <= src_height


def _render_crop(
    *,
    source_path: Path,
    x: int,
    y: int,
    w: int,
    h: int,
) -> bytes:
    """Synchronous worker — opens the source and encodes the crop.

    Returns the raw PNG bytes. Pillow's ``crop`` is lazy (it returns a
    view), so we ``load()`` the result to materialise the pixels before
    the ``with`` block closes the source file handle. We re-encode as
    PNG even if the source was a JPEG: callers want a lossless slice
    of the original, and the size bump on a small crop is negligible.
    """
    with Image.open(source_path) as src_img:
        src_img.load()
        cropped = src_img.crop((x, y, x + w, y + h))
        cropped.load()

    buffer = io.BytesIO()
    cropped.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


async def crop_png(shot_id: int, x: int, y: int, w: int, h: int) -> CropResult:
    """Return a cropped PNG of screenshot ``shot_id``.

    Args:
        shot_id: Primary key of the row in ``screenshots``.
        x: Left edge of the crop rectangle, in source pixels.
        y: Top edge of the crop rectangle, in source pixels.
        w: Width of the crop rectangle, in source pixels. Must be > 0.
        h: Height of the crop rectangle, in source pixels. Must be > 0.

    Returns:
        :class:`CropResult`. On ``status="ok"`` the ``png`` field holds
        a complete PNG body and ``size_bytes`` is its length. Non-ok
        statuses leave ``png`` as an empty bytes object so the caller
        cannot accidentally serve a partial image.
    """
    async with get_connection() as conn:
        shot = await get_screenshot(conn, shot_id)

    if shot is None:
        log.info("crop.shot_not_found", shot_id=shot_id)
        return CropResult(status="not_found", png=b"", size_bytes=0)

    if shot.thumbnail_path is None:
        log.info("crop.thumbnail_missing", shot_id=shot_id)
        return CropResult(status="missing_thumbnail", png=b"", size_bytes=0)

    source_path = Path(shot.thumbnail_path)
    if not source_path.is_file():
        log.warning(
            "crop.thumbnail_unreadable",
            shot_id=shot_id,
            source=str(source_path),
        )
        return CropResult(status="missing_thumbnail", png=b"", size_bytes=0)

    if not _validate_bounds(
        x=x,
        y=y,
        w=w,
        h=h,
        src_width=shot.width,
        src_height=shot.height,
    ):
        log.info(
            "crop.bad_bounds",
            shot_id=shot_id,
            x=x,
            y=y,
            w=w,
            h=h,
            src_width=shot.width,
            src_height=shot.height,
        )
        return CropResult(status="bad_bounds", png=b"", size_bytes=0)

    png_bytes = await anyio.to_thread.run_sync(
        lambda: _render_crop(
            source_path=source_path,
            x=x,
            y=y,
            w=w,
            h=h,
        )
    )

    log.info(
        "crop.built",
        shot_id=shot_id,
        x=x,
        y=y,
        w=w,
        h=h,
        size_bytes=len(png_bytes),
    )

    return CropResult(status="ok", png=png_bytes, size_bytes=len(png_bytes))


__all__ = ["CropResult", "crop_png"]
