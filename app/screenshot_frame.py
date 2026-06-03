"""Framed-screenshot share export (v0.72).

Persona's v0.72 share UI lets an operator hand out a signed link or an
embed snippet, but neither artefact looks particularly attractive when
dropped into a tweet, a Telegram chat, or a Slack DM — the bare
thumbnail is just an unframed bitmap and the URL unfurls into Persona's
admin chrome. This module produces the missing piece: a single,
self-contained PNG of one screenshot wrapped in a stylised "window
chrome" — macOS-style traffic-light dots, a faux URL bar, a thin
border, and a drop shadow — so the same image can be posted anywhere
without context and still read as "a screenshot from an app".

The public entry point :func:`build_framed_png` is **async on
purpose**: it loads the source thumbnail from disk and pushes every
synchronous Pillow call (open / paste / draw / save) onto a worker
thread via :func:`anyio.to_thread.run_sync`, so a slow PNG encode never
stalls uvicorn's event loop.

Design rules baked in:

* **Tolerant**, like :mod:`app.digest_card`. A missing thumbnail, a
  zero-byte file, a font-less environment — none of these raise. We
  always either write a frame or return a ``status`` other than
  ``"ok"`` so the route layer can translate it to an HTTP code instead
  of crashing.
* **Single style today, multi-style ready**. The ``style`` argument is
  a :data:`FrameStyle` literal; ``"mac"`` is the only value implemented
  in v0.72 but the dispatcher leaves the door open for future Windows
  or generic "browser" chromes without changing the public signature.
* **Pure-function output**. Given the same shot id and style, the
  bytes on disk are stable — callers can safely treat the result as a
  cacheable asset.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final, Literal, TypedDict

import anyio
from PIL import Image, ImageDraw, ImageFont

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, get_screenshot

log = get_logger("persona.frame")
log_watermark = get_logger("persona.frame.watermark")

# kv_settings key holding the operator-wide default watermark text.
# Empty / unset means "no watermark", matching the build_framed_png
# default. Per-request overrides come from the route layer.
_KV_WATERMARK: Final[str] = "framed_watermark"

# ---------------------------------------------------------------------------
# Style registry. Today there's only one entry; the dispatcher exists so
# adding a "win" or "browser" chrome later means a new dict key + render
# branch, not a churn through the public signature.
# ---------------------------------------------------------------------------

FrameStyle = Literal["mac"]
_SUPPORTED_STYLES: Final[frozenset[str]] = frozenset({"mac"})
_DEFAULT_STYLE: Final[FrameStyle] = "mac"

# ---------------------------------------------------------------------------
# Layout constants — every magic number in :func:`_render_frame` lives
# here so the chrome can be retuned without spelunking through draw
# calls. Pixel values were picked to look reasonable against the
# typical 900-px-wide Persona thumbnail while still degrading gracefully
# on the rare 320-px warm-tier thumbnail.
# ---------------------------------------------------------------------------

# Title bar height, in pixels. macOS-style: three traffic lights on the
# left, a pill-shaped URL field in the middle.
_HEADER_HEIGHT: Final[int] = 32

# Thin inner border around the screenshot itself — keeps the bitmap
# from kissing the chrome edges.
_BORDER: Final[int] = 8

# Drop-shadow geometry. The shadow is a single dark rectangle nudged
# right + down behind the chrome, intentionally simple — a true
# Gaussian blur would require either SciPy or several extra PIL passes
# and the visual delta isn't worth the dependency.
_SHADOW_OFFSET: Final[int] = 12
_SHADOW_COLOR: Final[tuple[int, int, int, int]] = (0, 0, 0, 110)

# Outer margin around the whole composition — gives social-media
# unfurls some breathing room when they crop tight.
_MARGIN: Final[int] = 24

# Traffic-light dots: classic close / minimise / maximise palette.
_DOT_RADIUS: Final[int] = 6
_DOT_GAP: Final[int] = 8
_DOT_LEFT_PADDING: Final[int] = 14
_DOT_VERTICAL_CENTRE_OFFSET: Final[int] = _HEADER_HEIGHT // 2
_DOT_COLOURS: Final[tuple[tuple[int, int, int], ...]] = (
    (255, 95, 86),  # close
    (255, 189, 46),  # minimise
    (39, 201, 63),  # maximise
)

# URL pill geometry.
_URL_PILL_HEIGHT: Final[int] = 18
_URL_PILL_VERTICAL_INSET: Final[int] = (_HEADER_HEIGHT - _URL_PILL_HEIGHT) // 2
_URL_PILL_HORIZONTAL_INSET_LEFT: Final[int] = (
    _DOT_LEFT_PADDING + 3 * (2 * _DOT_RADIUS) + 2 * _DOT_GAP + 16
)
_URL_PILL_HORIZONTAL_INSET_RIGHT: Final[int] = 14

# Palette. Light, neutral chrome so the screenshot inside stays the
# focal point. The header bar uses two tones so the URL pill reads as
# inset.
_CHROME_BG: Final[tuple[int, int, int]] = (236, 236, 240)
_CHROME_BORDER: Final[tuple[int, int, int]] = (200, 200, 208)
_URL_PILL_BG: Final[tuple[int, int, int]] = (255, 255, 255)
_URL_TEXT: Final[tuple[int, int, int]] = (90, 90, 100)
_CANVAS_BG: Final[tuple[int, int, int, int]] = (255, 255, 255, 0)
_INNER_BG: Final[tuple[int, int, int]] = (250, 250, 252)

# Maximum length of the URL-pill label. Anything longer is truncated
# with an ellipsis so a 200-character window title doesn't blow out the
# header.
_URL_LABEL_MAX: Final[int] = 64

# Watermark geometry. The watermark sits inside the screenshot area,
# anchored to the bottom-right corner with a small inset so it doesn't
# kiss the chrome edge. The fill is semi-transparent white on a faint
# dark backdrop so it stays legible on both light and dark screenshots
# without dominating the composition.
_WATERMARK_FONT_SIZE: Final[int] = 14
_WATERMARK_INSET: Final[int] = 10
_WATERMARK_PADDING_X: Final[int] = 6
_WATERMARK_PADDING_Y: Final[int] = 3
_WATERMARK_TEXT_FILL: Final[tuple[int, int, int, int]] = (255, 255, 255, 200)
_WATERMARK_BG_FILL: Final[tuple[int, int, int, int]] = (0, 0, 0, 90)
# Hard cap on watermark length so a runaway kv value can't blow out the
# image — anything past this is trimmed with an ellipsis before render.
_WATERMARK_MAX_CHARS: Final[int] = 80


class FrameResult(TypedDict):
    """Return payload for :func:`build_framed_png`.

    ``status`` values:

    * ``"ok"`` — file written, ``path`` and ``size_bytes`` are usable.
    * ``"not_found"`` — no such screenshot row.
    * ``"missing_thumbnail"`` — row exists but its thumbnail file is
      gone or unreadable.
    * ``"bad_style"`` — caller passed an unsupported ``style`` value.
    """

    status: str
    path: str | None
    size_bytes: int
    style: str


def _load_font(*, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Best-effort TrueType font with a bitmap fallback.

    Mirrors :func:`app.digest_card._load_font` — we try DejaVu first
    (present on every Linux container Persona ships into) and Arial as
    the Windows fallback. A truly font-less environment still renders
    text via Pillow's bundled bitmap font; the URL pill ends up uglier
    but the route never 500s.
    """
    for candidate in ("DejaVuSans.ttf", "Arial.ttf", "arial.ttf", "Helvetica.ttf"):
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _clip_url_label(label: str) -> str:
    """Trim ``label`` so it fits the URL pill without overflowing.

    The hard cap is :data:`_URL_LABEL_MAX` characters; anything longer
    is sliced and tagged with an ellipsis. We do **not** measure pixel
    width here — the pill is wide enough that 64 chars of any
    reasonable font fit, and a pixel-aware fit would complicate the
    test surface for no real win.
    """
    cleaned = label.strip() or "persona"
    if len(cleaned) <= _URL_LABEL_MAX:
        return cleaned
    return cleaned[: _URL_LABEL_MAX - 1] + "…"


def _draw_mac_header(
    draw: ImageDraw.ImageDraw,
    *,
    chrome_left: int,
    chrome_top: int,
    chrome_width: int,
    url_label: str,
    url_font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> None:
    """Render the macOS-style title bar: three dots + a URL pill.

    Coordinates are absolute on the outer canvas so the caller doesn't
    have to translate them — keeps the render function flat and easier
    to reason about.
    """
    # Header background.
    draw.rectangle(
        (
            chrome_left,
            chrome_top,
            chrome_left + chrome_width,
            chrome_top + _HEADER_HEIGHT,
        ),
        fill=_CHROME_BG,
    )

    # Traffic lights.
    dot_y = chrome_top + _DOT_VERTICAL_CENTRE_OFFSET
    for index, colour in enumerate(_DOT_COLOURS):
        cx = chrome_left + _DOT_LEFT_PADDING + _DOT_RADIUS + index * (2 * _DOT_RADIUS + _DOT_GAP)
        draw.ellipse(
            (cx - _DOT_RADIUS, dot_y - _DOT_RADIUS, cx + _DOT_RADIUS, dot_y + _DOT_RADIUS),
            fill=colour,
        )

    # URL pill — a rounded white rectangle with the placeholder text.
    pill_left = chrome_left + _URL_PILL_HORIZONTAL_INSET_LEFT
    pill_right = chrome_left + chrome_width - _URL_PILL_HORIZONTAL_INSET_RIGHT
    pill_top = chrome_top + _URL_PILL_VERTICAL_INSET
    pill_bottom = pill_top + _URL_PILL_HEIGHT
    if pill_right - pill_left >= 2 * _URL_PILL_HEIGHT:
        # Only draw the pill when there is room — on a tiny thumbnail
        # we'd otherwise produce an inverted rectangle and PIL throws.
        draw.rounded_rectangle(
            (pill_left, pill_top, pill_right, pill_bottom),
            radius=_URL_PILL_HEIGHT // 2,
            fill=_URL_PILL_BG,
            outline=_CHROME_BORDER,
            width=1,
        )
        text_x = pill_left + 10
        text_y = pill_top + 2
        draw.text((text_x, text_y), _clip_url_label(url_label), fill=_URL_TEXT, font=url_font)

    # 1-px bottom separator so the header reads as a distinct strip.
    draw.line(
        (
            chrome_left,
            chrome_top + _HEADER_HEIGHT,
            chrome_left + chrome_width,
            chrome_top + _HEADER_HEIGHT,
        ),
        fill=_CHROME_BORDER,
        width=1,
    )


def _clip_watermark(text: str) -> str:
    """Trim ``text`` to :data:`_WATERMARK_MAX_CHARS` with an ellipsis.

    Watermarks are operator input, so a stray 500-character paste must
    not push us into an unbounded canvas resize — we clip before render.
    """
    cleaned = text.strip()
    if len(cleaned) <= _WATERMARK_MAX_CHARS:
        return cleaned
    return cleaned[: _WATERMARK_MAX_CHARS - 1] + "…"


def _draw_watermark(
    canvas: Image.Image,
    *,
    inner_left: int,
    inner_top: int,
    inner_width: int,
    inner_height: int,
    text: str,
) -> None:
    """Stamp a semi-transparent watermark on the bottom-right of the shot.

    The watermark is drawn onto a dedicated RGBA overlay and then
    alpha-composited onto ``canvas`` so the translucent background pill
    blends cleanly with whatever pixels happen to live underneath it
    (dark terminal, light document, photo, etc.). When the requested
    text doesn't fit inside ``inner_width``/``inner_height`` we silently
    skip the stamp instead of clipping a half-painted glyph — better no
    mark than a broken one.
    """
    clipped = _clip_watermark(text)
    if not clipped:
        return

    font = _load_font(size=_WATERMARK_FONT_SIZE)
    # Use textbbox for both TrueType and bitmap fonts — Pillow normalises
    # the return shape so the same arithmetic works for either branch.
    measure_draw = ImageDraw.Draw(canvas)
    bbox = measure_draw.textbbox((0, 0), clipped, font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    pill_w = text_w + 2 * _WATERMARK_PADDING_X
    pill_h = text_h + 2 * _WATERMARK_PADDING_Y

    # Bail out when the screenshot is too small to host the watermark
    # without overlapping the chrome — a tiny 320-px thumbnail simply
    # won't carry a 14-px stamp legibly.
    if pill_w + 2 * _WATERMARK_INSET > inner_width:
        log_watermark.info(
            "frame.watermark.skipped_too_narrow",
            inner_width=inner_width,
            pill_width=pill_w,
        )
        return
    if pill_h + 2 * _WATERMARK_INSET > inner_height:
        log_watermark.info(
            "frame.watermark.skipped_too_short",
            inner_height=inner_height,
            pill_height=pill_h,
        )
        return

    pill_right = inner_left + inner_width - _WATERMARK_INSET
    pill_bottom = inner_top + inner_height - _WATERMARK_INSET
    pill_left = pill_right - pill_w
    pill_top = pill_bottom - pill_h

    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.rounded_rectangle(
        (pill_left, pill_top, pill_right, pill_bottom),
        radius=max(2, pill_h // 3),
        fill=_WATERMARK_BG_FILL,
    )
    # Subtract the bbox origin so the visible glyphs sit flush with the
    # padding — Pillow's textbbox can return negative top offsets for
    # fonts with ascender metrics.
    text_x = pill_left + _WATERMARK_PADDING_X - bbox[0]
    text_y = pill_top + _WATERMARK_PADDING_Y - bbox[1]
    overlay_draw.text((text_x, text_y), clipped, fill=_WATERMARK_TEXT_FILL, font=font)

    composited = Image.alpha_composite(canvas.convert("RGBA"), overlay)
    canvas.paste(composited, (0, 0))

    log_watermark.info(
        "frame.watermark.drawn",
        chars=len(clipped),
        pill_width=pill_w,
        pill_height=pill_h,
    )


def _render_frame(
    *,
    source_path: Path,
    output_path: Path,
    url_label: str,
    style: FrameStyle,
    watermark: str,
) -> int:
    """Synchronous worker — composes the frame and writes the PNG.

    Returns the on-disk size in bytes so the caller can log a one-line
    summary without a second ``stat`` call. Raises only for genuinely
    exceptional conditions (an unsupported ``style`` slipping past the
    public guard); missing-thumbnail cases are handled by the
    async wrapper before we get here.
    """
    if style not in _SUPPORTED_STYLES:  # pragma: no cover - guarded above
        msg = f"Unsupported frame style: {style}"
        raise ValueError(msg)

    with Image.open(source_path) as src_img:
        source = src_img.convert("RGB")

    inner_width = source.width
    inner_height = source.height
    chrome_width = inner_width + 2 * _BORDER
    chrome_height = inner_height + 2 * _BORDER + _HEADER_HEIGHT

    canvas_width = chrome_width + 2 * _MARGIN + _SHADOW_OFFSET
    canvas_height = chrome_height + 2 * _MARGIN + _SHADOW_OFFSET

    canvas = Image.new("RGBA", (canvas_width, canvas_height), _CANVAS_BG)

    # Shadow first, behind everything. A separate RGBA layer keeps the
    # alpha clean so the final flatten doesn't bleed translucent pixels
    # onto the chrome edges.
    shadow = Image.new("RGBA", (canvas_width, canvas_height), _CANVAS_BG)
    shadow_draw = ImageDraw.Draw(shadow)
    shadow_draw.rectangle(
        (
            _MARGIN + _SHADOW_OFFSET,
            _MARGIN + _SHADOW_OFFSET,
            _MARGIN + _SHADOW_OFFSET + chrome_width,
            _MARGIN + _SHADOW_OFFSET + chrome_height,
        ),
        fill=_SHADOW_COLOR,
    )
    canvas = Image.alpha_composite(canvas, shadow)

    draw = ImageDraw.Draw(canvas)

    chrome_left = _MARGIN
    chrome_top = _MARGIN

    # Outer chrome rectangle (the "window" itself).
    draw.rectangle(
        (chrome_left, chrome_top, chrome_left + chrome_width, chrome_top + chrome_height),
        fill=_INNER_BG,
        outline=_CHROME_BORDER,
        width=1,
    )

    url_font = _load_font(size=12)
    _draw_mac_header(
        draw,
        chrome_left=chrome_left,
        chrome_top=chrome_top,
        chrome_width=chrome_width,
        url_label=url_label,
        url_font=url_font,
    )

    # Paste the screenshot inside the border.
    inner_left = chrome_left + _BORDER
    inner_top = chrome_top + _HEADER_HEIGHT + _BORDER
    canvas.paste(
        source,
        (inner_left, inner_top),
    )

    if watermark:
        _draw_watermark(
            canvas,
            inner_left=inner_left,
            inner_top=inner_top,
            inner_width=inner_width,
            inner_height=inner_height,
            text=watermark,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)
    return output_path.stat().st_size


async def build_framed_png(
    shot_id: int,
    output_path: Path | str,
    *,
    style: FrameStyle = _DEFAULT_STYLE,
    watermark: str | None = None,
) -> FrameResult:
    """Render a stylised window-chrome frame around screenshot ``shot_id``.

    Args:
        shot_id: Primary key of the row in ``screenshots``.
        output_path: Destination PNG path. Parent directories are
            created on demand.
        style: Chrome style. ``"mac"`` is the only value implemented in
            v0.72; anything else returns ``status="bad_style"``.
        watermark: Optional bottom-right watermark text. ``None`` (the
            default) means "use whatever the operator stored in
            ``kv_settings`` under ``framed_watermark``"; an explicit
            empty string disables the watermark even when a kv default
            exists, so per-request overrides can fully suppress it.

    Returns:
        :class:`FrameResult`. On ``status="ok"`` the file at
        ``path`` is a complete, well-formed PNG and ``size_bytes`` is
        its on-disk size. Non-ok statuses leave the destination
        untouched.
    """
    if style not in _SUPPORTED_STYLES:
        log.warning("frame.bad_style", style=style, shot_id=shot_id)
        return FrameResult(
            status="bad_style",
            path=None,
            size_bytes=0,
            style=style,
        )

    async with get_connection() as conn:
        shot = await get_screenshot(conn, shot_id)
        # Only consult the kv default when the caller didn't pass an
        # explicit value (even an empty string counts as explicit — see
        # docstring) so the route layer can force "no watermark".
        if watermark is None:
            kv_value = await get_kv(conn, _KV_WATERMARK)
            watermark_text = (kv_value or "").strip()
        else:
            watermark_text = watermark.strip()

    if shot is None:
        log.info("frame.shot_not_found", shot_id=shot_id)
        return FrameResult(
            status="not_found",
            path=None,
            size_bytes=0,
            style=style,
        )

    if shot.thumbnail_path is None:
        log.info("frame.thumbnail_missing", shot_id=shot_id)
        return FrameResult(
            status="missing_thumbnail",
            path=None,
            size_bytes=0,
            style=style,
        )

    source_path = Path(shot.thumbnail_path)
    if not source_path.is_file():
        log.warning(
            "frame.thumbnail_unreadable",
            shot_id=shot_id,
            source=str(source_path),
        )
        return FrameResult(
            status="missing_thumbnail",
            path=None,
            size_bytes=0,
            style=style,
        )

    url_label = shot.app_name or "persona"

    out_path = Path(output_path)
    size_bytes = await anyio.to_thread.run_sync(
        lambda: _render_frame(
            source_path=source_path,
            output_path=out_path,
            url_label=url_label,
            style=style,
            watermark=watermark_text,
        )
    )

    log.info(
        "frame.built",
        shot_id=shot_id,
        style=style,
        path=str(out_path),
        url_label=url_label,
        size_bytes=size_bytes,
        watermark_chars=len(watermark_text),
    )

    return FrameResult(
        status="ok",
        path=str(out_path),
        size_bytes=size_bytes,
        style=style,
    )


__all__ = ["FrameResult", "FrameStyle", "build_framed_png"]
