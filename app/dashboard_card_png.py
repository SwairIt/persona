"""Dashboard share-card PNG — single 1200x630 day snapshot.

Renders one self-contained PNG summarising a day's Persona stats so it
can be dropped into Telegram / Slack / iMessage without unfurl support.
The card is intentionally tiny in scope (one day, four big numbers, one
streak line) — it's the social-friendly cousin of
:mod:`app.digest_card` (weekly) and :mod:`app.monthly_digest_card`.

Layout (top-to-bottom):

1. Heading ``Persona — YYYY-MM-DD`` in the operator's display palette.
2. A 2x2 grid of big numbers:
   - total shots
   - voice minutes (sum of ``audio_segment.duration_seconds`` ÷ 60)
   - unique apps (``COUNT(DISTINCT app_name)``)
   - top app (the ``app_name`` with the highest shot count, truncated)
3. Footer line with the current consecutive-day streak.
4. ``persona.local`` brand string in the bottom-right corner.

All disk + pixel work runs inside :func:`anyio.to_thread.run_sync` so
the caller's event loop never blocks. SQL is parametrised (the only
user-influenced value, ``day_iso``, threads through ``?`` placeholders).

If Pillow is not importable (``ImportError`` at module load) we still
expose :func:`build_card_png` — it logs once and returns ``b""`` so the
HTTP route can degrade to a 503 rather than 500.
"""

from __future__ import annotations

import io
from datetime import date, datetime
from typing import TYPE_CHECKING, Final

import anyio

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.streak import current_streak

if TYPE_CHECKING:  # pragma: no cover - typing only
    from PIL.Image import Image as _PILImage
    from PIL.ImageDraw import ImageDraw as _PILImageDraw
    from PIL.ImageFont import FreeTypeFont as _PILFreeTypeFont
    from PIL.ImageFont import ImageFont as _PILImageFont

    _AnyFont = _PILImageFont | _PILFreeTypeFont

log = get_logger("persona.dashboard_card")

# ---------------------------------------------------------------------------
# Pillow is declared in pyproject — but we still guard the import so a
# broken Pillow install degrades gracefully instead of taking the whole
# /dashboard tree down.
# ---------------------------------------------------------------------------
try:
    from PIL import Image, ImageDraw, ImageFont

    _PIL_AVAILABLE_INIT = True
except ImportError:  # pragma: no cover - exercised only when PIL absent
    _PIL_AVAILABLE_INIT = False

_PIL_AVAILABLE: Final[bool] = _PIL_AVAILABLE_INIT

# ---------------------------------------------------------------------------
# Layout constants — every magic number used by the renderer lives here.
# ---------------------------------------------------------------------------

CARD_WIDTH: Final[int] = 1200
CARD_HEIGHT: Final[int] = 630

# Dark indigo backdrop — matches the request and is close to the
# Tailwind ``--bg-canvas`` token used in ``base.html``.
_BG: Final[tuple[int, int, int]] = (0x0F, 0x17, 0x2A)  # #0f172a

# Foreground palette tuned for dark backgrounds.
_INK_HEADING: Final[tuple[int, int, int]] = (244, 244, 248)
_INK_VALUE: Final[tuple[int, int, int]] = (255, 255, 255)
_INK_LABEL: Final[tuple[int, int, int]] = (148, 163, 184)  # slate-400
_INK_ACCENT: Final[tuple[int, int, int]] = (167, 139, 250)  # accent-400
_INK_BRAND: Final[tuple[int, int, int]] = (100, 116, 139)  # slate-500
_INK_CELL: Final[tuple[int, int, int]] = (30, 41, 59)  # slate-800

_PADDING: Final[int] = 64
_GRID_TOP: Final[int] = 200
_GRID_BOTTOM: Final[int] = CARD_HEIGHT - 130
_CELL_GAP: Final[int] = 24

# Brand string in the bottom-right corner.
_BRAND: Final[str] = "persona.local"

# Top-app cell can blow out the layout if the app name is long. We clip
# anything past 18 characters with an ellipsis — enough room for
# ``"google-chrome.exe"`` but short enough that the big-number font
# (size 64) still fits the cell.
_TOP_APP_CHAR_LIMIT: Final[int] = 18


def _parse_day(day_iso: str | None) -> date:
    """Parse ``YYYY-MM-DD`` or fall back to today's local date.

    Raises :class:`ValueError` on malformed input so the route layer can
    surface a 400 rather than silently rendering "today".
    """
    if day_iso is None or day_iso == "":
        return datetime.now().astimezone().date()
    return datetime.strptime(day_iso, "%Y-%m-%d").date()


async def _gather_stats(day: date) -> dict[str, int | str]:
    """Pull the four headline numbers + streak for a single day.

    All queries are parametrised on the ISO day string; the day value
    itself is derived from :func:`_parse_day` which already validates
    the input format, so no string-interpolation paths exist.
    """
    day_iso = day.isoformat()

    async with get_connection() as conn:
        # Total shots for the day.
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots "
            "WHERE DATE(captured_at) = ?",
            (day_iso,),
        )
        row = await cursor.fetchone()
        total_shots = int(row["n"]) if row is not None else 0

        # Voice minutes — round to the nearest minute so we never show
        # a confusing "0 min" for 30 seconds of audio (round up small
        # values via int(... + 0.5)).
        cursor = await conn.execute(
            "SELECT COALESCE(SUM(duration_seconds), 0.0) AS total "
            "FROM audio_segment "
            "WHERE DATE(captured_at) = ?",
            (day_iso,),
        )
        row = await cursor.fetchone()
        voice_seconds = float(row["total"]) if row is not None else 0.0
        voice_minutes = int(voice_seconds / 60.0 + 0.5)

        # Unique apps — distinct, non-empty ``app_name``.
        cursor = await conn.execute(
            "SELECT COUNT(DISTINCT app_name) AS n FROM screenshots "
            "WHERE DATE(captured_at) = ? "
            "AND app_name IS NOT NULL AND app_name != ''",
            (day_iso,),
        )
        row = await cursor.fetchone()
        unique_apps = int(row["n"]) if row is not None else 0

        # Top app by shot count for the day.
        cursor = await conn.execute(
            "SELECT app_name, COUNT(*) AS n FROM screenshots "
            "WHERE DATE(captured_at) = ? "
            "AND app_name IS NOT NULL AND app_name != '' "
            "GROUP BY app_name ORDER BY n DESC LIMIT 1",
            (day_iso,),
        )
        row = await cursor.fetchone()
        top_app = str(row["app_name"]) if row is not None else "—"

    streak_payload = await current_streak()
    streak_days = int(streak_payload["days"])

    return {
        "total_shots": total_shots,
        "voice_minutes": voice_minutes,
        "unique_apps": unique_apps,
        "top_app": top_app,
        "streak_days": streak_days,
    }


def _load_font(*, size: int, bold: bool = False) -> _AnyFont:
    """Best-effort TrueType lookup, falling back to PIL's bitmap default.

    Mirrors :func:`app.digest_card._load_font` so card and dashboard
    snapshot stay visually consistent.
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


def _text_width(draw: _PILImageDraw, text: str, font: _AnyFont) -> int:
    """Width in pixels of ``text`` rendered with ``font``.

    Pillow ≥10 dropped ``font.getsize``; ``textbbox`` is the supported
    replacement and returns ``(left, top, right, bottom)``.
    """
    bbox = draw.textbbox((0, 0), text, font=font)
    return int(bbox[2] - bbox[0])


def _clip_top_app(name: str) -> str:
    """Truncate the top-app label so it fits inside one grid cell."""
    if len(name) <= _TOP_APP_CHAR_LIMIT:
        return name
    return name[: _TOP_APP_CHAR_LIMIT - 1] + "…"


def _draw_cell(
    draw: _PILImageDraw,
    *,
    x0: int,
    y0: int,
    x1: int,
    y1: int,
    label: str,
    value: str,
    value_font: _AnyFont,
    label_font: _AnyFont,
) -> None:
    """Render one cell of the 2x2 grid: rounded backdrop + value + label."""
    draw.rounded_rectangle((x0, y0, x1, y1), radius=18, fill=_INK_CELL)
    cell_w = x1 - x0
    value_w = _text_width(draw, value, value_font)
    label_w = _text_width(draw, label, label_font)
    # Vertically: value sits in the upper half, label below it. We
    # centre both horizontally inside the cell.
    value_x = x0 + (cell_w - value_w) // 2
    value_y = y0 + 38
    label_x = x0 + (cell_w - label_w) // 2
    label_y = y1 - 50
    draw.text((value_x, value_y), value, fill=_INK_VALUE, font=value_font)
    draw.text((label_x, label_y), label, fill=_INK_LABEL, font=label_font)


def _render_card(stats: dict[str, int | str], day_iso: str) -> bytes:
    """Synchronous worker — composes the card and returns PNG bytes."""
    canvas: _PILImage = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), _BG)
    draw: _PILImageDraw = ImageDraw.Draw(canvas)

    heading_font = _load_font(size=58, bold=True)
    value_font = _load_font(size=84, bold=True)
    value_font_small = _load_font(size=42, bold=True)  # for the top-app cell
    label_font = _load_font(size=24, bold=False)
    footer_font = _load_font(size=28, bold=True)
    brand_font = _load_font(size=22, bold=False)

    # Heading.
    heading = f"Persona — {day_iso}"
    draw.text((_PADDING, _PADDING), heading, fill=_INK_HEADING, font=heading_font)

    # 2x2 grid.
    grid_width = CARD_WIDTH - 2 * _PADDING
    grid_height = _GRID_BOTTOM - _GRID_TOP
    cell_w = (grid_width - _CELL_GAP) // 2
    cell_h = (grid_height - _CELL_GAP) // 2
    cells: list[tuple[str, str, _AnyFont]] = [
        ("Shots", str(stats["total_shots"]), value_font),
        ("Voice minutes", str(stats["voice_minutes"]), value_font),
        ("Unique apps", str(stats["unique_apps"]), value_font),
        ("Top app", _clip_top_app(str(stats["top_app"])), value_font_small),
    ]
    for index, (label, value, font) in enumerate(cells):
        col = index % 2
        row = index // 2
        x0 = _PADDING + col * (cell_w + _CELL_GAP)
        y0 = _GRID_TOP + row * (cell_h + _CELL_GAP)
        x1 = x0 + cell_w
        y1 = y0 + cell_h
        _draw_cell(
            draw,
            x0=x0,
            y0=y0,
            x1=x1,
            y1=y1,
            label=label,
            value=value,
            value_font=font,
            label_font=label_font,
        )

    # Footer line: streak.
    streak_days = int(stats["streak_days"])
    suffix = "day" if streak_days == 1 else "days"
    footer = f"Streak: {streak_days} {suffix}"
    footer_y = CARD_HEIGHT - _PADDING - 32
    draw.text((_PADDING, footer_y), footer, fill=_INK_ACCENT, font=footer_font)

    # Brand string, bottom-right.
    brand_w = _text_width(draw, _BRAND, brand_font)
    draw.text(
        (CARD_WIDTH - _PADDING - brand_w, footer_y + 6),
        _BRAND,
        fill=_INK_BRAND,
        font=brand_font,
    )

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


async def build_card_png(day_iso: str | None = None) -> bytes:
    """Render the day's dashboard share-card and return PNG bytes.

    Args:
        day_iso: ``YYYY-MM-DD`` for the target day. ``None`` (or an
            empty string) falls back to today's local date so the
            no-arg path Just Works for casual sharing.

    Returns:
        The PNG payload as raw bytes. On a malformed ``day_iso`` we
        log a warning and return ``b""`` so the route layer can map it
        to a 400. When Pillow is missing we log once and likewise
        return ``b""``.
    """
    if not _PIL_AVAILABLE:
        log.warning("dashboard_card.pil_missing")
        return b""

    try:
        day = _parse_day(day_iso)
    except ValueError:
        log.warning("dashboard_card.bad_date", day=day_iso)
        return b""

    stats = await _gather_stats(day)
    day_iso_normalised = day.isoformat()

    png_bytes = await anyio.to_thread.run_sync(
        _render_card, stats, day_iso_normalised
    )

    log.info(
        "dashboard_card.built",
        day=day_iso_normalised,
        total_shots=stats["total_shots"],
        voice_minutes=stats["voice_minutes"],
        unique_apps=stats["unique_apps"],
        streak_days=stats["streak_days"],
        size_bytes=len(png_bytes),
    )

    return png_bytes


__all__ = ["CARD_HEIGHT", "CARD_WIDTH", "build_card_png"]
