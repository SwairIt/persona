"""Physical Gaussian blur of OCR-detected sensitive regions.

Counterpart to :mod:`app.redaction` (text-only masking). The text path
stops a secret from showing up in search results, but the image at
``/screenshot/{id}`` would still leak it. This module reads pixel
coordinates from ``pytesseract.image_to_data`` and physically blurs
every word whose text matches an enabled regex in the ``redaction_rule``
table.

The original file is overwritten in place. Its format is preserved so
the existing capture pipeline (PNG / JPEG) keeps working unchanged.

Heavy work (Tesseract layout, PIL paste/save) runs in a worker thread
via :mod:`anyio.to_thread` so the calling coroutine never blocks the
event loop.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

import anyio
import pytesseract
from PIL import Image, ImageFilter

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection

if TYPE_CHECKING:
    from pathlib import Path

log = get_logger("persona.image_blur")

BBox = tuple[int, int, int, int]
"""``(left, top, width, height)`` in pixels, as returned by Tesseract."""

BLUR_RADIUS = 12


async def _list_enabled_patterns() -> list[tuple[str, str]]:
    """Return ``(name, pattern)`` for every enabled redaction rule."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT name, pattern FROM redaction_rule "
            "WHERE enabled = 1 ORDER BY created_at, name"
        )
        rows = await cursor.fetchall()
    return [(str(row["name"]), str(row["pattern"])) for row in rows]


def _compile_patterns(rules: list[tuple[str, str]]) -> list[re.Pattern[str]]:
    """Compile regex once; skip + log invalid ones so a single bad rule cannot derail blur."""
    compiled: list[re.Pattern[str]] = []
    for name, pattern in rules:
        try:
            compiled.append(re.compile(pattern))
        except re.error as exc:
            log.warning(
                "image_blur.bad_pattern",
                name=name,
                pattern=pattern,
                error=str(exc),
            )
    return compiled


def _collect_bboxes(
    ocr_data: dict[str, Any],
    patterns: list[re.Pattern[str]],
) -> list[BBox]:
    """Scan ``image_to_data`` output and accumulate boxes for matching words."""
    if not patterns:
        return []
    words = ocr_data.get("text") or []
    lefts = ocr_data.get("left") or []
    tops = ocr_data.get("top") or []
    widths = ocr_data.get("width") or []
    heights = ocr_data.get("height") or []
    n = min(len(words), len(lefts), len(tops), len(widths), len(heights))

    bboxes: list[BBox] = []
    for i in range(n):
        word = str(words[i]).strip()
        if not word:
            continue
        if not any(p.search(word) for p in patterns):
            continue
        try:
            left = int(lefts[i])
            top = int(tops[i])
            width = int(widths[i])
            height = int(heights[i])
        except (TypeError, ValueError):
            continue
        if width <= 0 or height <= 0:
            continue
        bboxes.append((left, top, width, height))
    return bboxes


def _run_image_to_data(image_path: Path, langs: str, tesseract_path: Path | None) -> dict[str, Any]:
    """Synchronous Tesseract layout call — invoked via :func:`anyio.to_thread.run_sync`."""
    if tesseract_path is not None:
        pytesseract.pytesseract.tesseract_cmd = str(tesseract_path)

    with Image.open(image_path) as image:
        image.load()
        data = pytesseract.image_to_data(
            image,
            lang=langs,
            output_type=pytesseract.Output.DICT,
        )
    return dict(data)


def _apply_blur(image_path: Path, bboxes: list[BBox]) -> None:
    """Crop, Gaussian-blur, and paste back every bbox; save in place, format preserved."""
    if not bboxes:
        return
    with Image.open(image_path) as image:
        image.load()
        original_format = image.format or "PNG"
        canvas = image.copy()

        canvas_width, canvas_height = canvas.size
        for left, top, width, height in bboxes:
            right = min(left + width, canvas_width)
            bottom = min(top + height, canvas_height)
            clamped_left = max(left, 0)
            clamped_top = max(top, 0)
            if right <= clamped_left or bottom <= clamped_top:
                continue
            region_box = (clamped_left, clamped_top, right, bottom)
            region = canvas.crop(region_box)
            blurred = region.filter(ImageFilter.GaussianBlur(radius=BLUR_RADIUS))
            canvas.paste(blurred, region_box)

        canvas.save(image_path, format=original_format)


async def blur_sensitive_regions(
    image_path: Path,
    ocr_data: dict[str, Any] | None = None,
) -> tuple[int, list[BBox]]:
    """Blur OCR words on ``image_path`` that match any enabled redaction rule.

    If ``ocr_data`` is not provided, runs ``pytesseract.image_to_data`` itself
    in a worker thread. Returns ``(regions_blurred, bboxes)``. A return value
    of ``(0, [])`` means nothing matched (or no rules are enabled) and the
    file is byte-identical to its original state.
    """
    if not image_path.exists():
        msg = f"image not found: {image_path}"
        raise FileNotFoundError(msg)

    rules = await _list_enabled_patterns()
    if not rules:
        return 0, []
    patterns = _compile_patterns(rules)
    if not patterns:
        return 0, []

    settings = get_settings()
    if ocr_data is None:
        ocr_data = await anyio.to_thread.run_sync(
            _run_image_to_data,
            image_path,
            settings.tesseract_langs,
            settings.tesseract_path,
        )

    bboxes = _collect_bboxes(ocr_data, patterns)
    if not bboxes:
        return 0, []

    await anyio.to_thread.run_sync(_apply_blur, image_path, bboxes)
    return len(bboxes), bboxes
