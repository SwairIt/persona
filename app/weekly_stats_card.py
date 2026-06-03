"""Weekly-stats share-card PNG — 1200x630 "last 7 days on Persona" recap.

A purely numeric companion to :mod:`app.digest_card`. Where the digest
card surfaces the LLM-written "Big themes" prose, this card focuses on
the cold facts a user actually wants to post when bragging about a
productive week — total captures, the app that dominated their time,
and the three keywords that bubbled to the top of OCR + notes.

The card is deterministically sized at 1200x630 (the de-facto
``og:image`` ratio) and intentionally reuses the dark-violet gradient
palette from the weekly digest card so a user posting both PNGs at
once sees a coherent visual set instead of two unrelated graphics.

Layout (left-to-right, top-to-bottom):

* heading ``Last 7 days on Persona``
* date range subheading (``YYYY-MM-DD → YYYY-MM-DD``)
* three giant numeric tiles — *captures*, *active hours*, *unique apps*
* "Top app" line with the dominant ``app_name`` and its share of time
* "Top keywords" block — bullet list of the three most-frequent tokens

Heavy work — ``ImageDraw.text``, ``Image.new``, font loading and the
final ``Image.save`` — runs inside :func:`anyio.to_thread.run_sync` so
the caller's event loop never blocks on pixel arithmetic or disk IO.

The function is intentionally tolerant: a freshly-installed Persona
with zero captures still renders a valid PNG (showing zeros and an
"—" placeholder) so the share endpoint never 500s on an empty DB.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Final, TypedDict

import anyio
from PIL import Image, ImageDraw, ImageFont

from app.keywords import top_keywords
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

log = get_logger("persona.weekly_stats_card")

# ---------------------------------------------------------------------------
# Layout constants — every magic number used by ``_render_card`` lives here
# so the layout can be retuned without spelunking through draw calls.
# ---------------------------------------------------------------------------

CARD_WIDTH: Final[int] = 1200
CARD_HEIGHT: Final[int] = 630

# Vertical gradient endpoints. Identical palette to ``app.digest_card``
# so the weekly-stats card and the weekly-digest card look like a
# matched pair when a user posts both to the same social timeline.
_GRADIENT_TOP: Final[tuple[int, int, int]] = (11, 13, 28)
_GRADIENT_BOTTOM: Final[tuple[int, int, int]] = (40, 22, 78)

# Foreground palette. Mirrors ``app.digest_card`` token-for-token.
_INK_HEADING: Final[tuple[int, int, int]] = (244, 244, 248)
_INK_BODY: Final[tuple[int, int, int]] = (212, 212, 220)
_INK_MUTED: Final[tuple[int, int, int]] = (148, 148, 168)
_INK_ACCENT: Final[tuple[int, int, int]] = (167, 139, 250)  # accent-400
_INK_TILE_BG: Final[tuple[int, int, int]] = (24, 22, 52)
_INK_TILE_EDGE: Final[tuple[int, int, int]] = (60, 50, 90)

# Generous gutter so the heading clears every social-card crop box we
# have observed in the wild (Slack/Telegram/Twitter all crop differently).
_PADDING: Final[int] = 64

# How many days the recap window covers. Hard-coded to 7 because the
# label literally says "Last 7 days"; if we ever ship a 14-day variant
# it lives in a separate module.
_WINDOW_DAYS: Final[int] = 7

# Keyword bullets shown under "Top keywords". The :func:`top_keywords`
# helper is asked for a few extras so we can drop empties without
# falling short of three visible rows.
_KEYWORDS_VISIBLE: Final[int] = 3
_KEYWORDS_FETCH: Final[int] = 8
_KEYWORD_CHAR_LIMIT: Final[int] = 30

# Tile geometry. Three side-by-side rounded rects under the heading.
_TILE_COUNT: Final[int] = 3
_TILE_GAP: Final[int] = 24
_TILE_HEIGHT: Final[int] = 180
_TILE_RADIUS: Final[int] = 18

# Gap-aware active-seconds attribution mirrors ``time_on_app`` so the
# "Top app" calculation lines up with what users see on the dashboard.
_MAX_GAP_SECONDS: Final[int] = 300

# Cap an app name so a runaway browser tab title can't push the share
# pill off the canvas. Trims with an ellipsis identical to the digest
# card's theme truncation for visual consistency.
_APP_NAME_LIMIT: Final[int] = 42


class WeeklyStatsCardResult(TypedDict):
    """Return payload for :func:`build_weekly_stats_card`."""

    status: str
    path: str | None
    start_date: str
    end_date: str
    width: int
    height: int
    total_shots: int
    active_seconds: int
    unique_apps: int
    top_app: str | None
    top_app_seconds: int
    keywords: list[str]
    size_bytes: int


class _CardData(TypedDict):
    start_date: date
    end_date: date
    total_shots: int
    active_seconds: int
    unique_apps: int
    top_app: str | None
    top_app_seconds: int
    keywords: list[str]


def _parse_end_date(end_date: str | date) -> date:
    """Coerce the public ``end_date`` argument to a real :class:`date`.

    Accepting both ``"YYYY-MM-DD"`` and a pre-parsed :class:`date` keeps
    the public surface friendly for callers wiring URL params (always
    strings) and CLI tooling (which often hands us ``date.today()``).
    """
    if isinstance(end_date, date) and not isinstance(end_date, datetime):
        return end_date
    if isinstance(end_date, datetime):
        return end_date.date()
    return datetime.strptime(str(end_date), "%Y-%m-%d").date()


def _trim(text: str, limit: int) -> str:
    """Flatten whitespace and cap to ``limit`` characters with an ellipsis."""
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1] + "…"


def _format_hours(seconds: int) -> str:
    """Render ``seconds`` as ``H:MM`` with no zero-padding on hours."""
    if seconds <= 0:
        return "0:00"
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f"{hours}:{minutes:02d}"


def _walk_active_seconds(
    rows: list[tuple[str, str]],
    max_gap_seconds: int,
) -> dict[str, int]:
    """Fold ordered ``(app_name, captured_at_iso)`` rows into per-app seconds.

    Same gap-capped attribution as :func:`app.time_on_app._walk_day_rows`
    but trimmed to "seconds only" — the share-card never displays shot
    counts per app, so we skip that bucket to keep the function small.

    Rows MUST already be partitioned by local day (gaps never bridge
    midnight) and sorted ascending by ``captured_at``.
    """
    seconds: dict[str, int] = {}
    prev_app: str | None = None
    prev_dt: datetime | None = None

    for app_raw, captured_at_raw in rows:
        if not app_raw:
            # Capture with unknown foreground app can't be attributed —
            # reset the streak so the next row doesn't claim the gap.
            prev_app = None
            prev_dt = None
            continue
        app = str(app_raw)
        when = datetime.fromisoformat(str(captured_at_raw))
        if prev_app == app and prev_dt is not None:
            diff = (when - prev_dt).total_seconds()
            if 0 < diff <= max_gap_seconds:
                seconds[app] = seconds.get(app, 0) + int(diff)
        else:
            # Make sure the app key exists even with zero attributed
            # seconds — otherwise a single-shot app gets dropped from
            # the "unique apps" count.
            seconds.setdefault(app, 0)
        prev_app = app
        prev_dt = when

    return seconds


async def _load_card_data(start_date: date, end_date: date) -> _CardData:
    """Pull totals, top-app and top-keywords for the inclusive window."""
    total_shots = 0
    per_app_seconds: dict[str, int] = {}

    # Window edges in UTC ISO 8601 form. ``end_dt`` is the exclusive
    # upper bound — start of the day *after* ``end_date`` — so a capture
    # made at 23:59:59 on the final day is still inside the window.
    start_dt = datetime.combine(start_date, time.min, tzinfo=UTC)
    end_dt = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=UTC)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots "
            "WHERE captured_at >= ? AND captured_at < ?",
            (iso(start_dt), iso(end_dt)),
        )
        row = await cursor.fetchone()
        total_shots = int(row["n"]) if row is not None else 0

        cursor = await conn.execute(
            "SELECT DATE(captured_at) AS day, app_name, captured_at "
            "FROM screenshots "
            "WHERE captured_at >= ? AND captured_at < ? "
            "ORDER BY day, captured_at",
            (iso(start_dt), iso(end_dt)),
        )
        raw_rows = await cursor.fetchall()

    # Partition by local day so the gap walk never bridges midnight,
    # mirroring ``app.time_on_app.app_summary``'s safety guarantee.
    per_day: dict[str, list[tuple[str, str]]] = {}
    for r in raw_rows:
        key = str(r["day"])
        per_day.setdefault(key, []).append(
            (
                str(r["app_name"]) if r["app_name"] is not None else "",
                str(r["captured_at"]),
            )
        )

    for day_rows in per_day.values():
        for app, secs in _walk_active_seconds(day_rows, _MAX_GAP_SECONDS).items():
            per_app_seconds[app] = per_app_seconds.get(app, 0) + secs

    if per_app_seconds:
        top_app, top_seconds = max(per_app_seconds.items(), key=lambda kv: kv[1])
    else:
        top_app, top_seconds = None, 0
    active_seconds = sum(per_app_seconds.values())
    unique_apps = len(per_app_seconds)

    raw_keywords = await top_keywords(days=_WINDOW_DAYS, top_n=_KEYWORDS_FETCH)
    keywords: list[str] = []
    for entry in raw_keywords:
        word = str(entry.get("word", "")).strip()
        if not word:
            continue
        keywords.append(_trim(word, _KEYWORD_CHAR_LIMIT))
        if len(keywords) >= _KEYWORDS_VISIBLE:
            break

    return {
        "start_date": start_date,
        "end_date": end_date,
        "total_shots": total_shots,
        "active_seconds": active_seconds,
        "unique_apps": unique_apps,
        "top_app": top_app,
        "top_app_seconds": top_seconds,
        "keywords": keywords,
    }


def _load_font(*, size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Best-effort TrueType font with bitmap fallback.

    Mirrors :func:`app.digest_card._load_font` so the two cards pick the
    same font family on every host Persona ships into. DejaVu wins on
    Linux containers; Arial picks up the slack on Windows; the bundled
    bitmap font is the absolute last resort so rendering never raises.
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
    O(height) work once per card on the worker thread; trivial next to
    text rasterisation cost.
    """
    pixels = canvas.load()
    if pixels is None:  # pragma: no cover - defensive
        return
    width = canvas.width
    height = canvas.height
    r0, g0, b0 = _GRADIENT_TOP
    r1, g1, b1 = _GRADIENT_BOTTOM
    span = max(1, height - 1)
    for y in range(height):
        t = y / span
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
    """Top block: marketing heading + ISO date range underneath."""
    draw.text(
        (_PADDING, _PADDING),
        "Last 7 days on Persona",
        fill=_INK_HEADING,
        font=heading_font,
    )
    subheading = (
        f"{data['start_date'].isoformat()} → {data['end_date'].isoformat()}"
    )
    draw.text(
        (_PADDING, _PADDING + 80),
        subheading,
        fill=_INK_MUTED,
        font=subheading_font,
    )


def _draw_tiles(
    draw: ImageDraw.ImageDraw,
    data: _CardData,
    *,
    value_font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    label_font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> None:
    """Three big-number tiles: captures, active hours, unique apps."""
    tiles_top = _PADDING + 140
    chart_width = CARD_WIDTH - 2 * _PADDING
    tile_width = (chart_width - _TILE_GAP * (_TILE_COUNT - 1)) // _TILE_COUNT
    cells: list[tuple[str, str]] = [
        (str(data["total_shots"]), "captures"),
        (_format_hours(data["active_seconds"]), "active hours"),
        (str(data["unique_apps"]), "unique apps"),
    ]
    for index, (value, label) in enumerate(cells):
        x0 = _PADDING + index * (tile_width + _TILE_GAP)
        y0 = tiles_top
        x1 = x0 + tile_width
        y1 = y0 + _TILE_HEIGHT
        draw.rounded_rectangle(
            (x0, y0, x1, y1),
            radius=_TILE_RADIUS,
            fill=_INK_TILE_BG,
            outline=_INK_TILE_EDGE,
            width=2,
        )
        value_w = _text_width(draw, value, value_font)
        # Centre the value horizontally inside the tile; vertical offset
        # is empirical (matches the optical centre of the chosen font).
        draw.text(
            (x0 + (tile_width - value_w) // 2, y0 + 36),
            value,
            fill=_INK_ACCENT,
            font=value_font,
        )
        label_w = _text_width(draw, label, label_font)
        draw.text(
            (x0 + (tile_width - label_w) // 2, y0 + _TILE_HEIGHT - 50),
            label,
            fill=_INK_MUTED,
            font=label_font,
        )


def _draw_top_app(
    draw: ImageDraw.ImageDraw,
    data: _CardData,
    *,
    label_font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    body_font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> None:
    """Single-line "Top app" row sitting under the tile strip."""
    row_y = _PADDING + 140 + _TILE_HEIGHT + 36
    draw.text(
        (_PADDING, row_y),
        "Top app",
        fill=_INK_ACCENT,
        font=label_font,
    )
    if data["top_app"]:
        share_pct = 0
        if data["active_seconds"] > 0:
            share_pct = round(100 * data["top_app_seconds"] / data["active_seconds"])
        app_label = _trim(data["top_app"], _APP_NAME_LIMIT)
        body = (
            f"{app_label}  ·  "
            f"{_format_hours(data['top_app_seconds'])}  ·  {share_pct}%"
        )
    else:
        body = "—"
    draw.text((_PADDING, row_y + 36), body, fill=_INK_BODY, font=body_font)


def _draw_keywords(
    draw: ImageDraw.ImageDraw,
    keywords: list[str],
    *,
    label_font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
    body_font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> None:
    """Right column listing the three most-frequent keywords."""
    col_x = CARD_WIDTH // 2 + 40
    row_y = _PADDING + 140 + _TILE_HEIGHT + 36
    draw.text(
        (col_x, row_y),
        "Top keywords",
        fill=_INK_ACCENT,
        font=label_font,
    )
    if not keywords:
        draw.text(
            (col_x, row_y + 36),
            "—",
            fill=_INK_MUTED,
            font=body_font,
        )
        return
    for idx, word in enumerate(keywords):
        draw.text(
            (col_x, row_y + 36 + idx * 42),
            f"·  {word}",
            fill=_INK_BODY,
            font=body_font,
        )


def _draw_brand(
    draw: ImageDraw.ImageDraw,
    *,
    brand_font: ImageFont.ImageFont | ImageFont.FreeTypeFont,
) -> None:
    """Tiny ``persona`` wordmark in the bottom-right corner."""
    brand = "persona"
    brand_w = _text_width(draw, brand, brand_font)
    draw.text(
        (CARD_WIDTH - _PADDING - brand_w, CARD_HEIGHT - _PADDING - 32),
        brand,
        fill=_INK_ACCENT,
        font=brand_font,
    )


def _render_card(data: _CardData, output_path: Path) -> int:
    """Synchronous worker — composes the card and writes the PNG.

    Returns the on-disk size in bytes so the async wrapper can log a
    one-line summary without an extra ``stat`` syscall.
    """
    canvas = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), _GRADIENT_TOP)
    _paint_gradient(canvas)
    draw = ImageDraw.Draw(canvas)

    heading_font = _load_font(size=58, bold=True)
    subheading_font = _load_font(size=26, bold=False)
    value_font = _load_font(size=72, bold=True)
    label_font = _load_font(size=24, bold=True)
    body_font = _load_font(size=28, bold=False)
    brand_font = _load_font(size=24, bold=True)

    _draw_heading(
        draw,
        data,
        heading_font=heading_font,
        subheading_font=subheading_font,
    )
    _draw_tiles(draw, data, value_font=value_font, label_font=label_font)
    _draw_top_app(draw, data, label_font=label_font, body_font=body_font)
    _draw_keywords(
        draw,
        data["keywords"],
        label_font=label_font,
        body_font=body_font,
    )
    _draw_brand(draw, brand_font=brand_font)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, format="PNG", optimize=True)
    return output_path.stat().st_size


async def build_weekly_stats_card(
    end_date: str | date,
    output_path: Path | str,
) -> WeeklyStatsCardResult:
    """Render the last-7-days share-card PNG and return its summary.

    Args:
        end_date: Last day of the recap window (inclusive). Accepts
            either a :class:`datetime.date` or a ``YYYY-MM-DD`` string;
            the window is ``[end_date - 6 days, end_date]``.
        output_path: Where to write the PNG. Parent directories are
            created on demand.

    Returns:
        :class:`WeeklyStatsCardResult`. ``status`` is ``"ok"`` whenever
        the file is written — even if the window has zero captures and
        no keywords — so the route layer can rely on the file existing.
        ``"bad_date"`` is returned for malformed input.
    """
    try:
        end = _parse_end_date(end_date)
    except (TypeError, ValueError):
        log.warning("weekly_stats_card.bad_date", end_date=str(end_date))
        return WeeklyStatsCardResult(
            status="bad_date",
            path=None,
            start_date="",
            end_date="",
            width=CARD_WIDTH,
            height=CARD_HEIGHT,
            total_shots=0,
            active_seconds=0,
            unique_apps=0,
            top_app=None,
            top_app_seconds=0,
            keywords=[],
            size_bytes=0,
        )

    start = end - timedelta(days=_WINDOW_DAYS - 1)
    data = await _load_card_data(start, end)

    out_path = Path(output_path)
    size_bytes = await anyio.to_thread.run_sync(_render_card, data, out_path)

    log.info(
        "weekly_stats_card.built",
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        path=str(out_path),
        total_shots=data["total_shots"],
        active_seconds=data["active_seconds"],
        unique_apps=data["unique_apps"],
        top_app=data["top_app"],
        keywords=len(data["keywords"]),
        size_bytes=size_bytes,
    )

    return WeeklyStatsCardResult(
        status="ok",
        path=str(out_path),
        start_date=start.isoformat(),
        end_date=end.isoformat(),
        width=CARD_WIDTH,
        height=CARD_HEIGHT,
        total_shots=data["total_shots"],
        active_seconds=data["active_seconds"],
        unique_apps=data["unique_apps"],
        top_app=data["top_app"],
        top_app_seconds=data["top_app_seconds"],
        keywords=data["keywords"],
        size_bytes=size_bytes,
    )


__all__ = [
    "CARD_HEIGHT",
    "CARD_WIDTH",
    "WeeklyStatsCardResult",
    "build_weekly_stats_card",
]
