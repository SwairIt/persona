"""Insight-specific share cards — single-PNG snapshots of one metric.

Where :mod:`app.dashboard_card_png` summarises a *whole day* in a 2x2
grid, this module focuses each card on **one** insight so the result is
boring enough to read at a glance and big enough to look good in a
Twitter / Telegram paste.

Four templates are exposed; every one is async, runs all heavy work
(SQL + Pillow) on threads, and returns raw PNG bytes.

* :func:`build_top_app_card` — "I spent N shots in <app> this week"
  for the ISO-8601 week starting at the supplied Monday.
* :func:`build_longest_focus_card` — longest single focus_session in
  the trailing week (label or app fallback), rendered as ``Hh MMm``.
* :func:`build_most_active_hour_card` — hour of day with the highest
  *average* shots per occurrence over the last 30 days.
* :func:`build_streak_card` — current consecutive-day capture streak
  together with the date of the very first capture.

Layout for every card: 1200x630, dark indigo background, big centred
number, smaller subtitle line beneath it, ``via Persona`` footer.

All SQL is parametrised; the only operator-supplied value
(``week_start_iso`` / ``day_iso``) is parsed through ``strptime`` first
so malformed input degrades to ``b""`` rather than rendering garbage.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timedelta
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

log = get_logger("persona.insight_cards")

# ---------------------------------------------------------------------------
# Pillow guard — mirrors dashboard_card_png.py. Missing PIL must not 500
# the whole /share tree.
# ---------------------------------------------------------------------------
try:
    from PIL import Image, ImageDraw, ImageFont

    _PIL_AVAILABLE_INIT = True
except ImportError:  # pragma: no cover - exercised only when PIL absent
    _PIL_AVAILABLE_INIT = False

_PIL_AVAILABLE: Final[bool] = _PIL_AVAILABLE_INIT

# ---------------------------------------------------------------------------
# Layout constants. Kept in one block so re-skinning is a one-stop edit.
# ---------------------------------------------------------------------------

CARD_WIDTH: Final[int] = 1200
CARD_HEIGHT: Final[int] = 630

# Dark indigo background — same #0f172a as the dashboard card so a paste
# of mixed cards still looks coherent.
_BG: Final[tuple[int, int, int]] = (0x0F, 0x17, 0x2A)

# Foreground palette — value uses white, label slate-400, accent purple
# for the eyebrow line above the headline number.
_INK_HEADING: Final[tuple[int, int, int]] = (244, 244, 248)
_INK_VALUE: Final[tuple[int, int, int]] = (255, 255, 255)
_INK_SUBTITLE: Final[tuple[int, int, int]] = (203, 213, 225)  # slate-300
_INK_ACCENT: Final[tuple[int, int, int]] = (167, 139, 250)  # accent-400
_INK_FOOTER: Final[tuple[int, int, int]] = (100, 116, 139)  # slate-500

_PADDING: Final[int] = 64
_BRAND: Final[str] = "via Persona"

# A long app name / file path would otherwise blow out the subtitle row.
# 36 chars fits well inside the 1200-px width at size-40.
_SUBTITLE_CHAR_LIMIT: Final[int] = 36

# Histogram window for the "most active hour" metric. 30 days matches
# the default in :func:`app.hour_histogram.hourly_distribution` so the
# share-card number stays consistent with the live page.
_HOUR_WINDOW_DAYS: Final[int] = 30

# Focus / capture-session lookback window for "longest focus session".
_FOCUS_WINDOW_DAYS: Final[int] = 7


# ---------------------------------------------------------------------------
# Tiny helpers shared by every renderer.
# ---------------------------------------------------------------------------


def _parse_day(day_iso: str | None) -> date:
    """Parse ``YYYY-MM-DD`` or fall back to today's local date.

    Raises :class:`ValueError` on malformed input — the public builders
    swallow that and return ``b""`` so the route layer maps it to a 400.
    """
    if day_iso is None or day_iso == "":
        return datetime.now().astimezone().date()
    return datetime.strptime(day_iso, "%Y-%m-%d").date()


def _week_bounds(week_start_iso: str | None) -> tuple[date, date]:
    """Return ``(monday, sunday)`` for the ISO week containing ``week_start_iso``.

    When ``week_start_iso`` is omitted we anchor on *this* week (Monday-
    aligned, local time). Any explicit value is snapped back to its
    Monday so an accidental mid-week input still produces a sane card.
    """
    if week_start_iso is None or week_start_iso == "":
        anchor = datetime.now().astimezone().date()
    else:
        anchor = datetime.strptime(week_start_iso, "%Y-%m-%d").date()
    monday = anchor - timedelta(days=anchor.weekday())
    return monday, monday + timedelta(days=6)


def _clip_subtitle(text: str) -> str:
    """Truncate subtitle text to keep it inside one card row."""
    if len(text) <= _SUBTITLE_CHAR_LIMIT:
        return text
    return text[: _SUBTITLE_CHAR_LIMIT - 1] + "…"


def _format_hms(seconds: int) -> str:
    """Format a positive duration in seconds as ``Hh MMm`` (or ``MMm SSs``)."""
    seconds = max(seconds, 0)
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes:02d}m"
    if minutes > 0:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def _format_hour_range(hour: int) -> str:
    """Return e.g. ``10:00-11:00`` for the integer hour 10."""
    nxt = (hour + 1) % 24
    return f"{hour:02d}:00-{nxt:02d}:00"


def _load_font(*, size: int, bold: bool = False) -> _AnyFont:
    """Best-effort TrueType lookup; defaults to PIL's bitmap if none found."""
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
    """Return the rendered width in pixels for ``text``+``font``."""
    bbox = draw.textbbox((0, 0), text, font=font)
    return int(bbox[2] - bbox[0])


def _draw_centred(
    draw: _PILImageDraw,
    *,
    text: str,
    y: int,
    font: _AnyFont,
    fill: tuple[int, int, int],
) -> None:
    """Horizontally centre ``text`` at the given ``y`` row."""
    width = _text_width(draw, text, font)
    x = (CARD_WIDTH - width) // 2
    draw.text((x, y), text, fill=fill, font=font)


def _compose_card(
    eyebrow: str,
    value: str,
    subtitle: str,
    footer_hint: str,
) -> bytes:
    """Render the shared layout: eyebrow + big number + subtitle + footer."""
    canvas: _PILImage = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), _BG)
    draw: _PILImageDraw = ImageDraw.Draw(canvas)

    eyebrow_font = _load_font(size=34, bold=True)
    value_font = _load_font(size=148, bold=True)
    subtitle_font = _load_font(size=40, bold=False)
    footer_font = _load_font(size=24, bold=False)
    brand_font = _load_font(size=26, bold=True)

    # A thin accent rule along the left edge so the card visually anchors
    # without competing with the centred number.
    draw.rectangle(
        (_PADDING - 28, _PADDING, _PADDING - 22, CARD_HEIGHT - _PADDING),
        fill=_INK_ACCENT,
    )

    _draw_centred(draw, text=eyebrow, y=_PADDING + 30, font=eyebrow_font, fill=_INK_ACCENT)
    _draw_centred(draw, text=value, y=_PADDING + 120, font=value_font, fill=_INK_VALUE)
    _draw_centred(
        draw,
        text=_clip_subtitle(subtitle),
        y=_PADDING + 310,
        font=subtitle_font,
        fill=_INK_SUBTITLE,
    )

    # Footer hint sits bottom-left, brand string bottom-right.
    footer_y = CARD_HEIGHT - _PADDING - 24
    draw.text((_PADDING, footer_y), footer_hint, fill=_INK_FOOTER, font=footer_font)
    brand_w = _text_width(draw, _BRAND, brand_font)
    draw.text(
        (CARD_WIDTH - _PADDING - brand_w, footer_y - 4),
        _BRAND,
        fill=_INK_HEADING,
        font=brand_font,
    )

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Top-app card.
# ---------------------------------------------------------------------------


async def _query_top_app(monday: date, sunday: date) -> tuple[str, int]:
    """Return ``(app_name, shot_count)`` for the busiest app in the week.

    Falls back to ``("—", 0)`` on an empty week. Parametrised SQL only.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, COUNT(*) AS n FROM screenshots "
            "WHERE DATE(captured_at) >= ? AND DATE(captured_at) <= ? "
            "AND app_name IS NOT NULL AND app_name != '' "
            "GROUP BY app_name ORDER BY n DESC LIMIT 1",
            (monday.isoformat(), sunday.isoformat()),
        )
        row = await cursor.fetchone()
    if row is None:
        return "—", 0
    return str(row["app_name"]), int(row["n"])


async def build_top_app_card(week_start_iso: str | None = None) -> bytes:
    """Render the "top app this week" insight card.

    Args:
        week_start_iso: ``YYYY-MM-DD`` for any day in the target ISO
            week — it is snapped to that week's Monday. ``None`` falls
            back to the current week.

    Returns:
        PNG bytes on success, ``b""`` on bad input or missing Pillow.
    """
    if not _PIL_AVAILABLE:
        log.warning("insight_cards.pil_missing", kind="top_app")
        return b""
    try:
        monday, sunday = _week_bounds(week_start_iso)
    except ValueError:
        log.warning("insight_cards.bad_week", kind="top_app", value=week_start_iso)
        return b""

    app_name, shot_count = await _query_top_app(monday, sunday)
    subtitle = f"in {app_name} this week" if shot_count else "no captures this week"
    footer = f"{monday.isoformat()} → {sunday.isoformat()}"

    png_bytes = await anyio.to_thread.run_sync(
        _compose_card,
        "TOP APP",
        str(shot_count),
        subtitle,
        footer,
    )

    log.info(
        "insight_cards.built",
        kind="top_app",
        week_start=monday.isoformat(),
        app=app_name,
        shots=shot_count,
        size_bytes=len(png_bytes),
    )
    return png_bytes


# ---------------------------------------------------------------------------
# Longest focus card.
# ---------------------------------------------------------------------------


async def _query_longest_focus(window_days: int) -> tuple[int, str]:
    """Return ``(duration_seconds, label)`` of the longest session in window.

    Prefers the v0.36 ``focus_session`` table because rows there carry an
    operator-supplied ``label`` (free-form, often a filename). Falls
    back to ``capture_session.dominant_app`` when no focus sessions are
    available — the auto-detected work block is still the right answer
    for users who haven't adopted the Pomodoro flow.
    """
    cutoff = (datetime.now().astimezone() - timedelta(days=window_days)).isoformat()
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT label, started_at, ended_at "
            "FROM focus_session "
            "WHERE ended_at IS NOT NULL AND started_at >= ? "
            "ORDER BY (julianday(ended_at) - julianday(started_at)) DESC "
            "LIMIT 1",
            (cutoff,),
        )
        focus_row = await cursor.fetchone()
        if focus_row is not None:
            started = datetime.fromisoformat(str(focus_row["started_at"]))
            ended = datetime.fromisoformat(str(focus_row["ended_at"]))
            seconds = int((ended - started).total_seconds())
            label_raw = focus_row["label"]
            label = str(label_raw) if label_raw not in (None, "") else "focus session"
            return seconds, label

        cursor = await conn.execute(
            "SELECT duration_seconds, dominant_app "
            "FROM capture_session "
            "WHERE started_at >= ? "
            "ORDER BY duration_seconds DESC LIMIT 1",
            (cutoff,),
        )
        cap_row = await cursor.fetchone()
    if cap_row is None:
        return 0, "no sessions yet"
    label_raw = cap_row["dominant_app"]
    label = str(label_raw) if label_raw not in (None, "") else "untitled session"
    return int(cap_row["duration_seconds"]), label


async def build_longest_focus_card(day_iso: str | None = None) -> bytes:
    """Render the "longest focus session this week" insight card.

    Args:
        day_iso: Optional anchor day; currently informational only —
            the query always looks at the trailing
            :data:`_FOCUS_WINDOW_DAYS` days from *now*. Kept in the
            signature so future "this week ending on X" tweaks don't
            break callers.

    Returns:
        PNG bytes on success, ``b""`` on bad input or missing Pillow.
    """
    if not _PIL_AVAILABLE:
        log.warning("insight_cards.pil_missing", kind="longest_focus")
        return b""
    try:
        anchor = _parse_day(day_iso)
    except ValueError:
        log.warning("insight_cards.bad_day", kind="longest_focus", value=day_iso)
        return b""

    seconds, label = await _query_longest_focus(_FOCUS_WINDOW_DAYS)
    value = _format_hms(seconds) if seconds > 0 else "—"
    subtitle = f"on {label}" if seconds > 0 else label
    footer = f"trailing {_FOCUS_WINDOW_DAYS} days · {anchor.isoformat()}"

    png_bytes = await anyio.to_thread.run_sync(
        _compose_card,
        "LONGEST FOCUS",
        value,
        subtitle,
        footer,
    )

    log.info(
        "insight_cards.built",
        kind="longest_focus",
        anchor=anchor.isoformat(),
        seconds=seconds,
        label=label,
        size_bytes=len(png_bytes),
    )
    return png_bytes


# ---------------------------------------------------------------------------
# Most-active-hour card.
# ---------------------------------------------------------------------------


async def _query_most_active_hour(window_days: int) -> tuple[int, int]:
    """Return ``(hour, avg_shots_per_active_day)`` for the busiest hour.

    "Average shots per day in which that hour had any captures" is more
    honest than ``count / window_days`` — it answers "when I am
    online, when am I most active". Falls back to ``(0, 0)`` when the
    window has no data.
    """
    modifier = f"-{window_days} days"
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT CAST(strftime('%H', captured_at) AS INTEGER) AS hr, "
            "COUNT(*) AS n, "
            "COUNT(DISTINCT DATE(captured_at)) AS days "
            "FROM screenshots "
            "WHERE captured_at IS NOT NULL "
            "AND captured_at >= date('now', ?) "
            "GROUP BY hr "
            "ORDER BY (CAST(COUNT(*) AS REAL) / "
            "MAX(COUNT(DISTINCT DATE(captured_at)), 1)) DESC "
            "LIMIT 1",
            (modifier,),
        )
        row = await cursor.fetchone()
    if row is None:
        return 0, 0
    hour_raw = row["hr"]
    if hour_raw is None:
        return 0, 0
    hour = int(hour_raw)
    days = int(row["days"]) if row["days"] else 1
    avg = int(int(row["n"]) / max(days, 1) + 0.5)
    return hour, avg


async def build_most_active_hour_card() -> bytes:
    """Render the "most productive hour" insight card.

    Returns:
        PNG bytes on success, ``b""`` if Pillow is missing.
    """
    if not _PIL_AVAILABLE:
        log.warning("insight_cards.pil_missing", kind="most_active_hour")
        return b""

    hour, avg_shots = await _query_most_active_hour(_HOUR_WINDOW_DAYS)
    if avg_shots == 0:
        value = "—"
        subtitle = "no captures in the last 30 days"
    else:
        value = _format_hour_range(hour)
        subtitle = f"avg {avg_shots} shots per active day"
    footer = f"window: last {_HOUR_WINDOW_DAYS} days"

    png_bytes = await anyio.to_thread.run_sync(
        _compose_card,
        "MOST PRODUCTIVE HOUR",
        value,
        subtitle,
        footer,
    )

    log.info(
        "insight_cards.built",
        kind="most_active_hour",
        hour=hour,
        avg_shots=avg_shots,
        size_bytes=len(png_bytes),
    )
    return png_bytes


# ---------------------------------------------------------------------------
# Streak card.
# ---------------------------------------------------------------------------


async def _query_first_capture() -> str | None:
    """Return the ISO date of the first-ever capture, or ``None`` if empty."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT MIN(DATE(captured_at)) AS first_day FROM screenshots "
            "WHERE captured_at IS NOT NULL"
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    first = row["first_day"]
    return str(first) if first else None


async def build_streak_card() -> bytes:
    """Render the "X-day capture streak" insight card.

    Returns:
        PNG bytes on success, ``b""`` if Pillow is missing.
    """
    if not _PIL_AVAILABLE:
        log.warning("insight_cards.pil_missing", kind="streak")
        return b""

    streak = await current_streak()
    first_capture = await _query_first_capture()
    days = int(streak["days"])
    longest = int(streak["longest"])

    value = str(days)
    if first_capture:
        subtitle = f"capture streak — first capture {first_capture}"
    else:
        subtitle = "capture streak — start one today"
    footer = f"all-time longest: {longest} days"

    png_bytes = await anyio.to_thread.run_sync(
        _compose_card,
        "DAILY STREAK",
        value,
        subtitle,
        footer,
    )

    log.info(
        "insight_cards.built",
        kind="streak",
        days=days,
        longest=longest,
        first_capture=first_capture,
        size_bytes=len(png_bytes),
    )
    return png_bytes


__all__ = [
    "CARD_HEIGHT",
    "CARD_WIDTH",
    "build_longest_focus_card",
    "build_most_active_hour_card",
    "build_streak_card",
    "build_top_app_card",
]
