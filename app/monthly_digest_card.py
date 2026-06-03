"""Monthly digest share-card PNG — 1200x630 Open Graph preview.

Renders a single shareable PNG for a monthly digest so the
``/digest/monthly-archive/{month}`` page has a rich social preview
(Twitter/X, Telegram, LinkedIn, Slack, Discord) without any third-party
service. The card is deterministically sized at 1200x630 — the de-facto
``og:image`` ratio — and uses an opaque dark gradient backdrop that
matches the weekly card from v0.62 so the two share-cards feel like a
coherent set.

Layout (left-to-right, top-to-bottom):

* heading ``Persona — month of {YYYY-MM}``
* top-3 themes pulled from the monthly digest's "Big themes" Markdown
  section (bullet text, truncated)
* per-day shot-count bars across the full calendar month (28-31 bars)
  drawn as a mini bar chart — strictly read-only, no labels per bar
  since 31 weekday labels would not fit
* totals strip at the bottom (month, total shots, top-1 theme)

Heavy work — ``ImageDraw.text``, ``Image.new``, font loading and the
final ``Image.save`` — runs inside :func:`anyio.to_thread.run_sync` so
the caller's event loop never blocks on pixel arithmetic or disk IO.

The function is intentionally tolerant: a missing digest row, an empty
"Big themes" section, zero captures in the month — none of these raise.
We always produce *some* card so the social preview never 500s on a
freshly-installed Persona that has never run the monthly LLM job.
"""

from __future__ import annotations

import calendar
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Final, TypedDict

import anyio
from PIL import Image, ImageDraw, ImageFont

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

log = get_logger("persona.monthly_card")

# ---------------------------------------------------------------------------
# Layout constants — every magic number used by ``_render_card`` lives here
# so the layout can be retuned without spelunking through draw calls.
# ---------------------------------------------------------------------------

CARD_WIDTH: Final[int] = 1200
CARD_HEIGHT: Final[int] = 630

# Vertical gradient endpoints. Identical palette to the weekly card so
# the two share-cards feel like a matched pair when posted together.
_GRADIENT_TOP: Final[tuple[int, int, int]] = (11, 13, 28)
_GRADIENT_BOTTOM: Final[tuple[int, int, int]] = (40, 22, 78)

# Foreground palette. Hex names mirror the Tailwind tokens in base.html
# so a future palette refresh stays in lockstep.
_INK_HEADING: Final[tuple[int, int, int]] = (244, 244, 248)
_INK_BODY: Final[tuple[int, int, int]] = (212, 212, 220)
_INK_MUTED: Final[tuple[int, int, int]] = (148, 148, 168)
_INK_ACCENT: Final[tuple[int, int, int]] = (167, 139, 250)  # accent-400
_INK_BAR: Final[tuple[int, int, int]] = (139, 92, 246)  # accent-500
_INK_BAR_DIM: Final[tuple[int, int, int]] = (60, 50, 90)

# Padding around the card edge. Generous on purpose — unfurl cards crop
# unpredictably and a 64-px gutter keeps glyphs clear of every crop box
# we've observed in the wild.
_PADDING: Final[int] = 64

# Bar chart dimensions. Sized so up to 31 bars + 30 gaps fit under the
# theme block with the totals strip still readable below.
_BAR_AREA_HEIGHT: Final[int] = 150
_BAR_GAP: Final[int] = 4
_BAR_MIN_HEIGHT: Final[int] = 4  # always visible, even for zero-count days

# Theme display cap. The "Big themes" section is markdown so we pull the
# first three bullet-or-line items; longer lists are dropped.
_THEMES_MAX: Final[int] = 3
_THEME_CHAR_LIMIT: Final[int] = 78

# Markdown heading we look for to extract themes from the digest body.
_THEMES_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s{0,3}#{1,3}\s+(?:big\s+themes|темы|big themes)\b.*$",
    re.IGNORECASE | re.MULTILINE,
)
# Leading bullet glyphs (-, *, •, 1., 1)) plus optional bold wrappers.
_BULLET_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?:[-*•]|\d+[.)])\s+(.+?)\s*$",
)
# Strip Markdown bold/italic/code/link wrappers from a bullet.
_INLINE_MD_RE: Final[re.Pattern[str]] = re.compile(r"[*_`]+")
_LINK_RE: Final[re.Pattern[str]] = re.compile(r"\[([^\]]+)\]\([^)]*\)")

# English month names so the heading reads "month of June 2026" even
# when ``calendar.month_name`` would localise to the host LC_TIME.
_MONTH_NAMES: Final[tuple[str, ...]] = (
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


class MonthlyCardResult(TypedDict):
    """Return payload for :func:`build_monthly_card`."""

    status: str
    path: str | None
    month: str
    width: int
    height: int
    themes: list[str]
    total_shots: int
    days_in_month: int
    size_bytes: int


class _CardData(TypedDict):
    month_iso: str
    year: int
    month: int
    days_in_month: int
    month_label: str
    themes: list[str]
    daily_counts: list[int]
    total_shots: int


def _parse_month_iso(month_iso: str) -> tuple[int, int]:
    """Parse ``YYYY-MM`` into ``(year, month)`` or raise :class:`ValueError`.

    Mirrors the validation in :mod:`app.web.routes.monthly_digests` so
    the card route and the detail page agree on which inputs are valid.
    """
    parts = month_iso.split("-")
    if len(parts) != 2 or len(parts[0]) != 4 or len(parts[1]) != 2:
        raise ValueError(f"Bad month format: {month_iso!r}")
    if not (parts[0].isdigit() and parts[1].isdigit()):
        raise ValueError(f"Bad month digits: {month_iso!r}")
    year = int(parts[0])
    month = int(parts[1])
    if not 1 <= month <= 12:
        raise ValueError(f"Month out of range: {month}")
    return year, month


def _extract_themes(body: str | None) -> list[str]:
    """Pull up to ``_THEMES_MAX`` items from the digest's "Big themes" section.

    The body is whatever the LLM produced — we cannot trust strict
    Markdown. Strategy:

    1. Locate the ``## Big themes`` heading (case-insensitive, allows
       Russian alias because the summariser writes in user's language).
    2. Walk lines until the next heading; collect bullet rows.
    3. If no bullets appear, fall back to non-empty lines so the user
       at least sees *something* in the card.

    Each line is stripped of common Markdown wrappers and truncated to
    ``_THEME_CHAR_LIMIT`` characters with an ellipsis so a runaway
    sentence never blows out the card layout.
    """
    if not body:
        return []

    match = _THEMES_HEADING_RE.search(body)
    if match is None:
        return []

    tail = body[match.end() :]
    next_heading = re.search(r"^\s{0,3}#{1,3}\s+\S", tail, re.MULTILINE)
    section = tail[: next_heading.start()] if next_heading else tail

    bullets: list[str] = []
    fallback: list[str] = []
    for raw in section.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        bullet_match = _BULLET_RE.match(raw)
        if bullet_match is not None:
            bullets.append(_clean_theme(bullet_match.group(1)))
        else:
            fallback.append(_clean_theme(stripped))
        if len(bullets) >= _THEMES_MAX:
            break

    chosen = bullets if bullets else fallback[:_THEMES_MAX]
    return [t for t in chosen if t][:_THEMES_MAX]


def _clean_theme(raw: str) -> str:
    """Strip Markdown decoration and clip to :data:`_THEME_CHAR_LIMIT`."""
    no_links = _LINK_RE.sub(r"\1", raw)
    no_md = _INLINE_MD_RE.sub("", no_links).strip()
    if len(no_md) <= _THEME_CHAR_LIMIT:
        return no_md
    return no_md[: _THEME_CHAR_LIMIT - 1] + "…"


async def _load_card_data(year: int, month: int) -> _CardData:
    """Fetch the digest body + per-day shot counts for one calendar month."""
    days_in_month = calendar.monthrange(year, month)[1]
    month_iso = f"{year:04d}-{month:02d}"
    month_first = date(year, month, 1)
    month_label = f"{_MONTH_NAMES[month]} {year}"

    body: str | None = None
    daily_counts: list[int] = [0] * days_in_month

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT body FROM monthly_digest WHERE month = ?",
            (month_iso,),
        )
        row = await cursor.fetchone()
        if row is not None:
            body = str(row["body"])

        # Per-day shot counts. SQLite ``DATE()`` extracts ``YYYY-MM-DD``
        # from the stored ISO-8601 timestamp; we group by it and merge
        # the result into the pre-allocated N-slot list so a day with
        # no captures stays at zero instead of going missing.
        start_dt = datetime.combine(month_first, time.min, tzinfo=UTC)
        end_dt = start_dt + timedelta(days=days_in_month)
        cursor = await conn.execute(
            "SELECT DATE(captured_at) AS day, COUNT(*) AS n "
            "FROM screenshots "
            "WHERE captured_at >= ? AND captured_at < ? "
            "GROUP BY DATE(captured_at)",
            (iso(start_dt), iso(end_dt)),
        )
        counts_by_day = {str(r["day"]): int(r["n"]) for r in await cursor.fetchall()}

    for offset in range(days_in_month):
        d = (month_first + timedelta(days=offset)).isoformat()
        daily_counts[offset] = counts_by_day.get(d, 0)
    total_shots = sum(daily_counts)

    themes = _extract_themes(body)

    return {
        "month_iso": month_iso,
        "year": year,
        "month": month,
        "days_in_month": days_in_month,
        "month_label": month_label,
        "themes": themes,
        "daily_counts": daily_counts,
        "total_shots": total_shots,
    }


def _load_font(*, size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Best-effort TrueType font, falling back to PIL's bitmap default.

    Mirrors :func:`app.digest_card._load_font` — we try DejaVu first
    (present on every Linux container Persona ships into) and Arial as
    the Windows fallback. A truly fontless environment still renders
    text via the bundled bitmap font; the result is uglier but the
    route never 500s.
    """
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


def _paint_gradient(canvas: Image.Image) -> None:
    """Vertical gradient backdrop — top dark ink → bottom accent violet.

    Painted row-by-row because Pillow has no native gradient primitive.
    The loop is O(height) and runs once per card on the worker thread,
    so the cost is negligible compared with text rasterisation.
    """
    pixels = canvas.load()
    if pixels is None:  # pragma: no cover - defensive
        return
    height = canvas.height
    width = canvas.width
    r0, g0, b0 = _GRADIENT_TOP
    r1, g1, b1 = _GRADIENT_BOTTOM
    for y in range(height):
        t = y / max(1, height - 1)
        r = round(r0 + (r1 - r0) * t)
        g = round(g0 + (g1 - g0) * t)
        b = round(b0 + (b1 - b0) * t)
        for x in range(width):
            pixels[x, y] = (r, g, b)


def _text_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> int:
    """Width in pixels of ``text`` rendered with ``font``.

    Pillow ≥10 dropped ``font.getsize``; ``textbbox`` is the supported
    replacement and returns ``(left, top, right, bottom)``.
    """
    bbox = draw.textbbox((0, 0), text, font=font)
    return int(bbox[2] - bbox[0])


def _draw_heading(
    draw: ImageDraw.ImageDraw,
    data: _CardData,
    *,
    heading_font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    subheading_font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> None:
    """Top block: "Persona — month of …" + ISO month code underneath."""
    heading = f"Persona — month of {data['month_label']}"
    draw.text((_PADDING, _PADDING), heading, fill=_INK_HEADING, font=heading_font)
    subheading = (
        f"{data['month_iso']}  ·  {data['days_in_month']} days"
    )
    draw.text(
        (_PADDING, _PADDING + 80),
        subheading,
        fill=_INK_MUTED,
        font=subheading_font,
    )


def _draw_themes(
    draw: ImageDraw.ImageDraw,
    themes: list[str],
    *,
    subheading_font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    body_font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> None:
    """Top-3 themes block, or a muted placeholder when the section is empty."""
    themes_top = _PADDING + 140
    if not themes:
        draw.text(
            (_PADDING, themes_top),
            "No monthly summary yet",
            fill=_INK_MUTED,
            font=subheading_font,
        )
        return
    draw.text(
        (_PADDING, themes_top),
        "Top themes",
        fill=_INK_ACCENT,
        font=subheading_font,
    )
    for idx, theme in enumerate(themes):
        y = themes_top + 44 + idx * 46
        draw.text((_PADDING, y), f"·  {theme}", fill=_INK_BODY, font=body_font)


def _draw_bars(
    draw: ImageDraw.ImageDraw,
    counts: list[int],
    *,
    label_font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> None:
    """Per-day shot count bar chart spanning the whole month (28-31 bars).

    Drawn without per-bar weekday labels because there's no room to
    print 31 of them — instead we tag the first and last day under the
    chart and annotate the single tallest bar with its count so the
    busiest day is legible at a glance.
    """
    bars = len(counts)
    if bars == 0:
        return
    bars_bottom = CARD_HEIGHT - _PADDING - 70
    bars_top = bars_bottom - _BAR_AREA_HEIGHT
    chart_width = CARD_WIDTH - 2 * _PADDING
    bar_width = max(4, (chart_width - _BAR_GAP * (bars - 1)) // bars)
    max_count = max(counts) if any(counts) else 0
    peak_index = counts.index(max_count) if max_count > 0 else -1
    for index, count in enumerate(counts):
        x0 = _PADDING + index * (bar_width + _BAR_GAP)
        x1 = x0 + bar_width
        if max_count > 0:
            height = max(
                _BAR_MIN_HEIGHT, round(_BAR_AREA_HEIGHT * count / max_count)
            )
        else:
            height = _BAR_MIN_HEIGHT
        y0 = bars_bottom - height
        draw.rectangle((x0, bars_top, x1, bars_bottom), fill=_INK_BAR_DIM)
        draw.rectangle((x0, y0, x1, bars_bottom), fill=_INK_BAR)
        if index == peak_index and count > 0:
            count_text = str(count)
            count_w = _text_width(draw, count_text, label_font)
            text_y = max(bars_top - 24, y0 - 24)
            text_x = x0 + (bar_width - count_w) // 2
            # Clamp so the peak label cannot escape the card gutter.
            text_x = max(_PADDING, min(text_x, CARD_WIDTH - _PADDING - count_w))
            draw.text(
                (text_x, text_y),
                count_text,
                fill=_INK_ACCENT,
                font=label_font,
            )
    # Range labels under the bar chart: "01" on the left, last day on the
    # right. Two anchors are enough for a 30-bar strip.
    first_label = "01"
    last_label = f"{bars:02d}"
    last_w = _text_width(draw, last_label, label_font)
    draw.text(
        (_PADDING, bars_bottom + 8),
        first_label,
        fill=_INK_MUTED,
        font=label_font,
    )
    draw.text(
        (CARD_WIDTH - _PADDING - last_w, bars_bottom + 8),
        last_label,
        fill=_INK_MUTED,
        font=label_font,
    )


def _draw_totals(
    draw: ImageDraw.ImageDraw,
    data: _CardData,
    *,
    totals_font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> None:
    """Bottom strip: shot total + truncated top theme + ``persona`` brand."""
    totals_y = CARD_HEIGHT - _PADDING - 32
    top_theme = data["themes"][0] if data["themes"] else "—"
    suffix = "…" if len(top_theme) > 54 else ""
    totals = (
        f"{data['total_shots']} captures  ·  top: {top_theme[:54]}{suffix}"
    )
    draw.text((_PADDING, totals_y), totals, fill=_INK_HEADING, font=totals_font)
    brand = "persona"
    brand_w = _text_width(draw, brand, totals_font)
    draw.text(
        (CARD_WIDTH - _PADDING - brand_w, totals_y),
        brand,
        fill=_INK_ACCENT,
        font=totals_font,
    )


def _render_card(data: _CardData, output_path: Path) -> int:
    """Synchronous worker — composes the card and writes the PNG.

    Returns the on-disk size in bytes so the caller can log a one-line
    summary without a second ``stat`` call.
    """
    canvas = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), _GRADIENT_TOP)
    _paint_gradient(canvas)
    draw = ImageDraw.Draw(canvas)

    heading_font = _load_font(size=52, bold=True)
    subheading_font = _load_font(size=26, bold=False)
    body_font = _load_font(size=30, bold=False)
    label_font = _load_font(size=20, bold=False)
    totals_font = _load_font(size=24, bold=True)

    _draw_heading(
        draw, data, heading_font=heading_font, subheading_font=subheading_font
    )
    _draw_themes(
        draw,
        data["themes"],
        subheading_font=subheading_font,
        body_font=body_font,
    )
    _draw_bars(draw, data["daily_counts"], label_font=label_font)
    _draw_totals(draw, data, totals_font=totals_font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)
    return output_path.stat().st_size


async def build_monthly_card(
    month_iso: str,
    output_path: Path | str,
) -> MonthlyCardResult:
    """Render the monthly digest share-card PNG and return its summary.

    Args:
        month_iso: ``YYYY-MM`` calendar month identifier.
        output_path: Where to write the PNG. Parent directories are
            created on demand.

    Returns:
        :class:`MonthlyCardResult`. ``status`` is ``"ok"`` whenever the
        file is written — even if the month has zero captures and no
        digest body — so the route layer can rely on the file existing.
        ``"bad_date"`` is returned for malformed input.
    """
    try:
        year, month = _parse_month_iso(month_iso)
    except ValueError:
        log.warning("monthly_card.bad_date", month=month_iso)
        return MonthlyCardResult(
            status="bad_date",
            path=None,
            month="",
            width=CARD_WIDTH,
            height=CARD_HEIGHT,
            themes=[],
            total_shots=0,
            days_in_month=0,
            size_bytes=0,
        )

    data = await _load_card_data(year, month)

    out_path = Path(output_path)
    size_bytes = await anyio.to_thread.run_sync(_render_card, data, out_path)

    log.info(
        "monthly_card.built",
        month=data["month_iso"],
        path=str(out_path),
        themes=len(data["themes"]),
        total_shots=data["total_shots"],
        days_in_month=data["days_in_month"],
        size_bytes=size_bytes,
    )

    return MonthlyCardResult(
        status="ok",
        path=str(out_path),
        month=data["month_iso"],
        width=CARD_WIDTH,
        height=CARD_HEIGHT,
        themes=data["themes"],
        total_shots=data["total_shots"],
        days_in_month=data["days_in_month"],
        size_bytes=size_bytes,
    )


__all__ = ["CARD_HEIGHT", "CARD_WIDTH", "MonthlyCardResult", "build_monthly_card"]
