"""Sample dominant background + foreground colours from an OCR bbox.

Companion to the per-word data captured by ``image_to_data`` in
:mod:`app.workers.ocr_worker`. The OCR worker calls
:func:`sample_colours` once per word (capped, first N per shot) with
the word's bounding box; the returned hex pair feeds the ``bg_hex`` /
``fg_hex`` columns added by migration ``049_ocr_word_colours.sql``.

The classification is intentionally crude — ``PIL.Image.quantize`` with
``colors=2`` collapses the crop to two palette entries. Whichever
cluster has more pixels is treated as the background (the surface
behind the glyph); the other becomes the foreground (the ink). This
breaks down on word-art / heavy anti-aliasing edge cases, but is
robust enough for the UI search use-case ("find shots with red error
text on a dark surface") that the colour columns exist to power.

Every PIL operation is wrapped in a broad ``try/except`` — a zero-area
crop, a non-RGB source image, a Pillow version mismatch, anything —
returns ``(None, None)``. The caller (worker side-channel) treats that
as "skip this row" and moves on; the main OCR pipeline never sees the
failure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.logging_setup import get_logger

if TYPE_CHECKING:
    from PIL import Image

log = get_logger("persona.ocr.colour")


def sample_colours(
    image: Image.Image,
    left: int,
    top: int,
    width: int,
    height: int,
) -> tuple[str | None, str | None]:
    """Return ``(bg_hex, fg_hex)`` for the bbox or ``(None, None)`` on failure.

    Hex format is ``#rrggbb`` (lowercase, leading ``#``). The background
    is the more-common pixel cluster after 2-colour quantization; the
    foreground is the other. If quantization collapses to a single
    cluster (uniform crop), both slots return the same hex — that is a
    valid result, not an error.

    Sync, CPU-bound: call from a worker thread (``anyio.to_thread`` or
    ``asyncio.to_thread``), never from the event loop directly.
    """
    try:
        if width <= 0 or height <= 0:
            return (None, None)

        right = left + width
        bottom = top + height
        crop = image.crop((left, top, right, bottom))
        if crop.size[0] <= 0 or crop.size[1] <= 0:
            return (None, None)

        # ``quantize`` needs a single-band-or-RGB source; normalise first so
        # palette / RGBA / L crops all collapse to RGB cleanly.
        rgb = crop.convert("RGB")
        quantized = rgb.quantize(colors=2)
        raw_palette = quantized.getpalette()
        palette: list[int] = [int(v) for v in raw_palette] if raw_palette else []
        # ``getcolors`` on a palette image returns ``[(count, palette_index), ...]``
        # — the second slot is the int palette index (not an RGB triple).
        # PIL's stubs widen the second slot's type; we coerce explicitly.
        raw_counts = quantized.getcolors() or []
        counts: list[tuple[int, int]] = []
        for entry in raw_counts:
            count_val, index_val = entry
            try:
                counts.append((int(count_val), int(index_val)))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
        if not counts:
            return (None, None)

        counts.sort(key=lambda item: item[0], reverse=True)
        bg_index = counts[0][1]
        fg_index = counts[1][1] if len(counts) > 1 else bg_index

        bg_hex = _palette_hex(palette, bg_index)
        fg_hex = _palette_hex(palette, fg_index)
        if bg_hex is None or fg_hex is None:
            return (None, None)
        return (bg_hex, fg_hex)
    except Exception as exc:
        log.debug(
            "ocr.colour.sample_failed",
            error=str(exc),
            left=left,
            top=top,
            width=width,
            height=height,
        )
        return (None, None)


def _palette_hex(palette: list[int], index: int) -> str | None:
    """Extract the ``#rrggbb`` triplet at ``index`` from a flat RGB palette."""
    base = index * 3
    if base < 0 or base + 2 >= len(palette):
        return None
    r = palette[base] & 0xFF
    g = palette[base + 1] & 0xFF
    b = palette[base + 2] & 0xFF
    return f"#{r:02x}{g:02x}{b:02x}"
