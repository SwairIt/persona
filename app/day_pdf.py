"""Per-day PDF export — single-file PDF of the same content as ``/export/day/{day}.md``.

This module is the v1.45 sibling of :mod:`app.day_markdown`. It reuses
:func:`app.day_markdown.build_day_md` to produce the comprehensive day
journal markdown body and then rasterises that text into a multi-page
PDF that the user can hand to a printer / email attachment / archive
without depending on any browser or external converter.

Design choices
~~~~~~~~~~~~~~

* **Pillow-only.** The task forbids touching ``pyproject.toml`` and the
  project already depends on Pillow for share cards. Pillow does not
  natively *render* PDF, but it ships a perfectly usable PDF *writer*:
  ``Image.save(buffer, format="PDF", append_images=[...])`` accepts an
  RGB ``PIL.Image`` and appends each follow-up frame as one page. We
  draw text into those PIL images with :class:`PIL.ImageDraw.ImageDraw`
  and let Pillow stitch them together.

* **Monospace + word wrap at ~95 chars.** The markdown body is shipped
  as plain pre-formatted text (the same way a ``less`` reader would
  see it), wrapped at 95 columns so a 1240px-wide A4 page (~150 DPI)
  fits every line without a horizontal scroll-equivalent or font
  shrinking.  Tables, code fences and bullet lists all stay readable.

* **Cover page.** Page 1 is a title sheet with the brand bar, the day
  heading (``Persona — YYYY-MM-DD``), the export timestamp and the
  page count footer. The body pages start at page 2.

* **Async + threadpool.** All Pillow work happens inside
  :func:`anyio.to_thread.run_sync` so the FastAPI worker is not
  blocked while the PDF is being assembled.

The function returns the PDF as ``bytes`` (matching the way
:mod:`app.dashboard_card_png` returns PNG bytes); the route layer adds
the ``Content-Disposition`` header and ships the bytes back to the
browser.
"""

from __future__ import annotations

import io
import textwrap
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

import anyio

from app.day_markdown import build_day_md
from app.logging_setup import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image as _PILImage
    from PIL.ImageDraw import ImageDraw as _PILImageDraw
    from PIL.ImageFont import FreeTypeFont as _PILFreeTypeFont
    from PIL.ImageFont import ImageFont as _PILImageFont

    _AnyFont = _PILImageFont | _PILFreeTypeFont

log = get_logger("persona.day_pdf")

# ---------------------------------------------------------------------------
# Pillow guard — match the dashboard_card_png pattern so a broken Pillow
# install degrades gracefully instead of taking the whole /export tree
# down. We re-raise from :func:`build_day_pdf` so the HTTP route can
# turn this into a 503.
# ---------------------------------------------------------------------------
try:
    from PIL import Image, ImageDraw, ImageFont

    _PIL_AVAILABLE_INIT = True
except ImportError:  # pragma: no cover - exercised only when PIL absent
    _PIL_AVAILABLE_INIT = False

_PIL_AVAILABLE: Final[bool] = _PIL_AVAILABLE_INIT


# ---------------------------------------------------------------------------
# Page geometry — A4 at ~150 DPI.
# ---------------------------------------------------------------------------

PAGE_WIDTH: Final[int] = 1240
PAGE_HEIGHT: Final[int] = 1754

# Inner content box — the rest is breathing room around the page edge.
_MARGIN_X: Final[int] = 80
_MARGIN_TOP: Final[int] = 100
_MARGIN_BOTTOM: Final[int] = 110

# Monospace body sizing. 16 px at 150 DPI is roughly 8 pt — small enough
# to fit ~60 lines per page and large enough to stay legible after a
# laser print.
_BODY_FONT_SIZE: Final[int] = 16
_BODY_LINE_HEIGHT: Final[int] = 22

# Cover-page typography.
_COVER_TITLE_FONT_SIZE: Final[int] = 64
_COVER_SUBTITLE_FONT_SIZE: Final[int] = 28
_COVER_FOOTER_FONT_SIZE: Final[int] = 20

# Per-page footer.
_FOOTER_FONT_SIZE: Final[int] = 18

# Word wrap width. 95 chars on a 1240px-wide page leaves comfortable
# breathing room either side at the chosen monospace size.
_WRAP_WIDTH: Final[int] = 95

# Lines per body page. Computed once so :func:`_paginate` does not
# repeat the arithmetic and the constant survives refactors.
_BODY_LINES_PER_PAGE: Final[int] = (
    PAGE_HEIGHT - _MARGIN_TOP - _MARGIN_BOTTOM
) // _BODY_LINE_HEIGHT

# Colour palette — light theme so printed pages do not waste a tank of
# toner. Mirrors the share-card light style at a higher contrast level.
_BG: Final[tuple[int, int, int]] = (255, 255, 255)
_INK_BODY: Final[tuple[int, int, int]] = (24, 24, 27)  # near-black
_INK_TITLE: Final[tuple[int, int, int]] = (15, 23, 42)  # slate-900
_INK_MUTED: Final[tuple[int, int, int]] = (100, 116, 139)  # slate-500
_INK_BRAND: Final[tuple[int, int, int]] = (124, 58, 237)  # accent purple
_BRAND_BAR_HEIGHT: Final[int] = 12

_BRAND_LABEL: Final[str] = "Persona"


# ---------------------------------------------------------------------------
# Font loading — best-effort TrueType lookup with bitmap fallback.
# ---------------------------------------------------------------------------


def _load_mono_font(size: int) -> _AnyFont:
    """Return a monospace TrueType font of ``size`` px or PIL's default.

    Tries DejaVu Sans Mono first (ships with Pillow on most Linux distros
    and Windows builds), then a few platform-specific aliases, and
    finally falls back to PIL's bitmap default. The fallback does not
    honour ``size`` but the body still renders — just with cramped
    glyphs — which is preferable to a 500.
    """
    candidates = (
        "DejaVuSansMono.ttf",
        "Consolas.ttf",
        "consola.ttf",
        "Courier New.ttf",
        "cour.ttf",
    )
    for candidate in candidates:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _load_sans_font(size: int, *, bold: bool = False) -> _AnyFont:
    """Return a proportional sans font of ``size`` px or PIL's default."""
    candidates_bold = (
        "DejaVuSans-Bold.ttf",
        "Arial Bold.ttf",
        "arialbd.ttf",
        "Helvetica-Bold.ttf",
    )
    candidates_regular = (
        "DejaVuSans.ttf",
        "Arial.ttf",
        "arial.ttf",
        "Helvetica.ttf",
    )
    for candidate in candidates_bold if bold else candidates_regular:
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Word-wrap helper — preserves blank lines so paragraphs stay separated.
# ---------------------------------------------------------------------------


def _wrap_markdown(body: str, *, width: int = _WRAP_WIDTH) -> list[str]:
    """Wrap each line of ``body`` to ``width`` columns, keeping blanks.

    :class:`textwrap.TextWrapper` is invoked once per source line so a
    blank line stays a single empty string (rather than being collapsed
    away by ``textwrap.wrap("")``). Markdown table rows, code fences and
    headings are left intact unless they exceed ``width``, in which case
    a soft break is inserted — the underlying markdown is not modified,
    only its on-page presentation.
    """
    wrapper = textwrap.TextWrapper(
        width=width,
        break_long_words=True,
        break_on_hyphens=False,
        replace_whitespace=False,
        drop_whitespace=False,
    )
    out: list[str] = []
    for raw_line in body.splitlines():
        stripped = raw_line.rstrip()
        if not stripped:
            out.append("")
            continue
        wrapped = wrapper.wrap(stripped)
        if not wrapped:
            out.append("")
            continue
        out.extend(wrapped)
    return out


def _paginate(lines: list[str], *, per_page: int) -> list[list[str]]:
    """Slice ``lines`` into page-sized buckets of ``per_page`` lines each.

    Returns at least one page (an empty body is rendered as a single
    "blank" page so the cover is never the only page in the PDF).
    """
    if not lines:
        return [[]]
    pages: list[list[str]] = []
    for index in range(0, len(lines), per_page):
        pages.append(lines[index : index + per_page])
    return pages


# ---------------------------------------------------------------------------
# Page renderers — each returns a fresh RGB :class:`PIL.Image.Image`.
# ---------------------------------------------------------------------------


def _new_page() -> _PILImage:
    """Allocate a blank A4-ish RGB page and paint the brand bar on it."""
    canvas: _PILImage = Image.new("RGB", (PAGE_WIDTH, PAGE_HEIGHT), _BG)
    draw: _PILImageDraw = ImageDraw.Draw(canvas)
    draw.rectangle(
        (0, 0, PAGE_WIDTH, _BRAND_BAR_HEIGHT),
        fill=_INK_BRAND,
    )
    return canvas


def _draw_page_footer(
    draw: _PILImageDraw,
    *,
    page_number: int,
    page_total: int,
    day_iso: str,
    font: _AnyFont,
) -> None:
    """Draw the per-page footer (day · page X / Y) at the bottom margin."""
    footer = f"{_BRAND_LABEL} {day_iso}  -  page {page_number} / {page_total}"
    bbox = draw.textbbox((0, 0), footer, font=font)
    text_width = int(bbox[2] - bbox[0])
    x = (PAGE_WIDTH - text_width) // 2
    y = PAGE_HEIGHT - _MARGIN_BOTTOM + 40
    draw.text((x, y), footer, fill=_INK_MUTED, font=font)


def _render_cover(
    *,
    day_iso: str,
    page_total: int,
    generated_at: str,
) -> _PILImage:
    """Render the first page — heading + subtitle + generation stamp."""
    canvas: _PILImage = _new_page()
    draw: _PILImageDraw = ImageDraw.Draw(canvas)

    title_font = _load_sans_font(_COVER_TITLE_FONT_SIZE, bold=True)
    subtitle_font = _load_sans_font(_COVER_SUBTITLE_FONT_SIZE, bold=False)
    footer_font = _load_sans_font(_COVER_FOOTER_FONT_SIZE, bold=False)

    title = f"{_BRAND_LABEL} - {day_iso}"
    subtitle = "Day journal export"
    stamp = f"Generated {generated_at}  -  {page_total} pages"

    # Centred vertically in the upper third of the page.
    title_bbox = draw.textbbox((0, 0), title, font=title_font)
    title_w = int(title_bbox[2] - title_bbox[0])
    title_x = (PAGE_WIDTH - title_w) // 2
    title_y = int(PAGE_HEIGHT * 0.28)
    draw.text((title_x, title_y), title, fill=_INK_TITLE, font=title_font)

    subtitle_bbox = draw.textbbox((0, 0), subtitle, font=subtitle_font)
    subtitle_w = int(subtitle_bbox[2] - subtitle_bbox[0])
    subtitle_x = (PAGE_WIDTH - subtitle_w) // 2
    subtitle_y = title_y + _COVER_TITLE_FONT_SIZE + 32
    draw.text(
        (subtitle_x, subtitle_y),
        subtitle,
        fill=_INK_MUTED,
        font=subtitle_font,
    )

    # Bottom strip — stamp + brand bar reference.
    stamp_bbox = draw.textbbox((0, 0), stamp, font=footer_font)
    stamp_w = int(stamp_bbox[2] - stamp_bbox[0])
    stamp_x = (PAGE_WIDTH - stamp_w) // 2
    stamp_y = PAGE_HEIGHT - _MARGIN_BOTTOM
    draw.text((stamp_x, stamp_y), stamp, fill=_INK_MUTED, font=footer_font)

    return canvas


def _render_body_page(
    *,
    lines: list[str],
    page_number: int,
    page_total: int,
    day_iso: str,
) -> _PILImage:
    """Render one body page with the given subset of wrapped lines."""
    canvas: _PILImage = _new_page()
    draw: _PILImageDraw = ImageDraw.Draw(canvas)

    body_font = _load_mono_font(_BODY_FONT_SIZE)
    footer_font = _load_sans_font(_FOOTER_FONT_SIZE, bold=False)

    y = _MARGIN_TOP
    for line in lines:
        draw.text((_MARGIN_X, y), line, fill=_INK_BODY, font=body_font)
        y += _BODY_LINE_HEIGHT

    _draw_page_footer(
        draw,
        page_number=page_number,
        page_total=page_total,
        day_iso=day_iso,
        font=footer_font,
    )
    return canvas


# ---------------------------------------------------------------------------
# Pillow PDF assembly.
# ---------------------------------------------------------------------------


def _render_pdf_sync(*, day_iso: str, body: str) -> bytes:
    """Synchronous worker — assembles the multi-page PDF in memory.

    The first frame is saved with ``format="PDF"`` and the rest are
    handed to ``append_images``; Pillow walks the list and produces a
    single byte string that we return to the caller verbatim. Note
    that ``save_all=True`` is required even though ``append_images`` is
    present — without it Pillow silently writes a one-page PDF.
    """
    if not _PIL_AVAILABLE:  # pragma: no cover - defensive
        raise RuntimeError("Pillow is not installed; cannot render PDF.")

    wrapped = _wrap_markdown(body)
    body_pages = _paginate(wrapped, per_page=_BODY_LINES_PER_PAGE)
    page_total = 1 + len(body_pages)
    generated_at = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")

    cover = _render_cover(
        day_iso=day_iso,
        page_total=page_total,
        generated_at=generated_at,
    )
    body_images: list[_PILImage] = [
        _render_body_page(
            lines=lines,
            page_number=index + 2,
            page_total=page_total,
            day_iso=day_iso,
        )
        for index, lines in enumerate(body_pages)
    ]

    buffer = io.BytesIO()
    cover.save(
        buffer,
        format="PDF",
        save_all=True,
        append_images=body_images,
        resolution=150.0,
    )
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


async def build_day_pdf(day_iso: str) -> bytes:
    """Return the per-day PDF for ``day_iso`` as ``bytes``.

    ``day_iso`` must be a ``YYYY-MM-DD`` literal; anything else raises
    :class:`ValueError` (propagated from :func:`app.day_markdown.build_day_md`)
    so the route layer can surface a 400. Returns ``b""`` when the day
    has no exportable content — the caller is expected to treat that as
    a 404 rather than serve a one-page placeholder PDF.
    """
    body = await build_day_md(day_iso)
    if not body:
        log.info("day_pdf.empty", day=day_iso)
        return b""

    if not _PIL_AVAILABLE:
        log.error("day_pdf.missing_pillow", day=day_iso)
        raise RuntimeError("Pillow is not installed; cannot render PDF.")

    payload = await anyio.to_thread.run_sync(
        lambda: _render_pdf_sync(day_iso=day_iso, body=body),
    )
    log.info(
        "day_pdf.built",
        day=day_iso,
        bytes=len(payload),
        markdown_bytes=len(body.encode("utf-8")),
    )
    return payload


__all__ = ["build_day_pdf"]
