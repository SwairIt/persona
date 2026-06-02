"""Visual diff thumbnail — a 320x180 PNG showing the pixel delta of two shots.

The output is a fixed-size, contrast-enhanced ``PIL.ImageChops.difference``
between the two source thumbnails, suitable as a small "what actually moved"
preview embedded under the v0.33 slider page. Compared to the slider (which
shows the two raw images stacked), the diff thumbnail collapses the answer
into a single glance: a mostly-black image means "barely changed", a bright
splotch points exactly at the region that moved.

The render is deterministic in ``(shot_a, shot_b)`` and cheap (two file
reads, two resizes, one difference, one contrast enhance, one PNG write)
so callers can regenerate on demand into a temp file rather than
persisting yet another artefact in the data tree.

All blocking PIL work is funnelled through :func:`anyio.to_thread.run_sync`
so the calling coroutine never stalls the event loop on disk IO.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, TypedDict

import anyio
from PIL import Image, ImageChops, ImageEnhance

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot

log = get_logger("persona.visual_diff")

# Fixed output dimensions — 320x180 is the same 16:9 footprint the rest of
# the app uses for "thumbnail-of-a-thumbnail" decorations, and matches the
# aspect ratio of a typical desktop capture. Anything wider would dominate
# the diff slider page; anything narrower would lose useful detail.
_THUMB_WIDTH: Final[int] = 320
_THUMB_HEIGHT: Final[int] = 180
_THUMB_SIZE: Final[tuple[int, int]] = (_THUMB_WIDTH, _THUMB_HEIGHT)

# 2x contrast — empirically the sweet spot. 1.0 leaves the raw difference
# (often too dim to read at this size); 4x clips the highlights into a
# solid blob and loses structure. Pinned so future tuning is a single edit.
_CONTRAST_FACTOR: Final[float] = 2.0

# LANCZOS keeps thin UI lines crisp through the downscale; pinned so a
# future Pillow default change cannot silently regress sharpness.
_RESAMPLE = Image.Resampling.LANCZOS


class VisualDiffResult(TypedDict):
    """Return payload for :func:`generate_diff_thumbnail`."""

    status: str
    path: str | None
    size_bytes: int


def _resolve_thumbnail(raw: str | None) -> Path | None:
    """Return a readable filesystem path for a stored ``thumbnail_path``.

    Mirrors the resolver used by :mod:`app.day_collage` and
    :mod:`app.pdf_export`: production rows store absolute paths, but older
    rows may have stored a cwd-relative form, so we try both before
    giving up. ``None`` is propagated so callers can short-circuit the
    "missing thumbnail" branch.
    """
    if raw is None:
        return None
    candidate = Path(raw)
    if candidate.is_file():
        return candidate
    if not candidate.is_absolute():
        rooted = Path.cwd() / candidate
        if rooted.is_file():
            return rooted
    return None


def _render_diff(
    path_a: Path,
    path_b: Path,
    output_path: Path,
) -> int:
    """Synchronous worker — composes the diff PNG and writes it to disk.

    Returns the on-disk size in bytes. Invoked via
    :func:`anyio.to_thread.run_sync` because every PIL call here
    (``open`` / ``resize`` / ``difference`` / ``enhance`` / ``save``) is
    blocking.

    Both source images are forced to ``RGB`` before the ``difference``
    call: ``ImageChops.difference`` requires matching modes and the
    stored thumbnails may be ``RGBA`` (WebP-with-alpha) or ``L``
    (occasional grayscale captures).
    """
    with Image.open(path_a) as src_a, Image.open(path_b) as src_b:
        src_a.load()
        src_b.load()
        frame_a = src_a.convert("RGB").resize(_THUMB_SIZE, _RESAMPLE)
        frame_b = src_b.convert("RGB").resize(_THUMB_SIZE, _RESAMPLE)

    diff = ImageChops.difference(frame_a, frame_b)
    enhanced = ImageEnhance.Contrast(diff).enhance(_CONTRAST_FACTOR)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    enhanced.save(output_path, format="PNG", optimize=True)
    return output_path.stat().st_size


async def generate_diff_thumbnail(
    shot_a_id: int,
    shot_b_id: int,
    output_path: Path | str,
) -> VisualDiffResult:
    """Render a 320x180 pixel-diff PNG for two screenshots.

    Args:
        shot_a_id: Database id of the "before" screenshot.
        shot_b_id: Database id of the "after" screenshot.
        output_path: Where to write the PNG. Parent directories are created.

    Returns:
        :class:`VisualDiffResult` whose ``status`` is:

        * ``"ok"`` — both thumbnails resolved, PNG written.
        * ``"missing"`` — at least one screenshot row is gone, or its
          ``thumbnail_path`` could not be located on disk. Nothing is
          written and ``path`` is ``None``.

    The diff is purely a function of the two input pixel grids, so a
    given ``(shot_a_id, shot_b_id)`` always produces an identical PNG —
    safe for ``Cache-Control: immutable`` at the route layer.
    """
    async with get_connection() as conn:
        shot_a = await get_screenshot(conn, shot_a_id)
        shot_b = await get_screenshot(conn, shot_b_id)

    if shot_a is None or shot_b is None:
        missing = [
            sid for sid, shot in ((shot_a_id, shot_a), (shot_b_id, shot_b))
            if shot is None
        ]
        log.info("visual_diff.shot_missing", missing=missing)
        return VisualDiffResult(status="missing", path=None, size_bytes=0)

    path_a = _resolve_thumbnail(shot_a.thumbnail_path)
    path_b = _resolve_thumbnail(shot_b.thumbnail_path)
    if path_a is None or path_b is None:
        log.info(
            "visual_diff.thumbnail_missing",
            id_a=shot_a_id,
            id_b=shot_b_id,
            has_a=path_a is not None,
            has_b=path_b is not None,
        )
        return VisualDiffResult(status="missing", path=None, size_bytes=0)

    out_path = Path(output_path)

    try:
        size_bytes = await anyio.to_thread.run_sync(
            _render_diff, path_a, path_b, out_path
        )
    except (OSError, ValueError) as exc:
        # OSError covers "file vanished" plus PIL's UnidentifiedImageError
        # (subclass of OSError); ValueError covers truncated/bad-mode files.
        log.warning(
            "visual_diff.render_failed",
            id_a=shot_a_id,
            id_b=shot_b_id,
            error=str(exc),
        )
        return VisualDiffResult(status="missing", path=None, size_bytes=0)

    log.info(
        "visual_diff.rendered",
        id_a=shot_a_id,
        id_b=shot_b_id,
        path=str(out_path),
        size_bytes=size_bytes,
    )
    return VisualDiffResult(status="ok", path=str(out_path), size_bytes=size_bytes)


__all__ = ["VisualDiffResult", "generate_diff_thumbnail"]
