"""Embeddable sparkline SVG endpoint — ``GET /api/sparkline.svg``.

Returns a pure-SVG (``image/svg+xml``) 320x60 polyline of per-day shot
counts for a FTS query over the trailing ``days`` window (default 30).

The endpoint is deliberately render-agnostic — no template engine, no
JavaScript, no client-side chart library. Callers embed the URL directly
in an ``<img>`` tag (or fetch it for inline SVG injection):

.. code-block:: html

    <img src="/api/sparkline.svg?q=standup&amp;days=30" alt="standup trend">

All user input enters SQL as a bound parameter — the FTS5 ``MATCH``
phrase is sanitised first to a safe subset of FTS5 syntax (word chars,
quotes, prefix wildcards), and the date window is a clamped integer
folded into a constant SQLite modifier string (``-N days``) — no part of
the user's request reaches the SQL string itself.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import TypedDict

from fastapi import APIRouter, Query
from fastapi.responses import Response

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.sparkline_svg")

router = APIRouter(tags=["sparkline-svg"])

# SVG geometry — fixed 320x60 polyline, mirrors the per-tag sparkline so
# both visuals line up when embedded side-by-side on dashboards.
_SVG_WIDTH = 320
_SVG_HEIGHT = 60
_PAD_X = 4
_PAD_Y = 6
_STROKE = "#a78bfa"  # accent-400
_FILL_BAND = "rgba(167, 139, 250, 0.15)"
_BASELINE_STROKE = "#3f3f46"  # zinc-700
_BASELINE_Y = _SVG_HEIGHT - _PAD_Y

_MIN_DAYS = 1
_MAX_DAYS = 365
_DEFAULT_DAYS = 30

_CACHE_MAX_AGE = 300  # seconds — five minutes of edge / browser caching.

_ISO_DATE_FMT = "%Y-%m-%d"

# Same character class as :func:`app.search.queries._sanitise_query` —
# allow word chars, whitespace, quoted phrases, prefix dashes, columns
# and dots; strip anything else that could break the FTS5 parser.
_FTS_SAFE = re.compile(r"[^\w\s\"\-\.\:АЯЁа-яё]+", re.UNICODE)


class _DayCount(TypedDict):
    date: str
    count: int


def _sanitise_fts(query: str) -> str:
    """Reduce free-form user text to a safe FTS5 ``MATCH`` phrase.

    Bare words become prefix matches (``foo`` → ``foo*``) for the same
    user-friendly behaviour as the main search page. Anything containing
    explicit FTS operators (``"``, ``-``, ``:``) is passed through.
    """
    cleaned = _FTS_SAFE.sub(" ", query).strip()
    if not cleaned:
        return ""
    if any(ch in cleaned for ch in '"-:'):
        return cleaned
    tokens = [token for token in cleaned.split() if token]
    return " ".join(f"{token}*" for token in tokens)


def _clamp_days(days: int) -> int:
    """Clamp ``days`` into the safe public range."""
    if days < _MIN_DAYS:
        return _MIN_DAYS
    if days > _MAX_DAYS:
        return _MAX_DAYS
    return days


def _dense_window(days: int, today: date | None = None) -> list[str]:
    """Return ``days`` ISO date strings ending at ``today`` (inclusive)."""
    end = today or date.today()
    start = end - timedelta(days=days - 1)
    return [
        (start + timedelta(days=offset)).strftime(_ISO_DATE_FMT)
        for offset in range(days)
    ]


async def _fetch_counts(fts_query: str, days: int) -> dict[str, int]:
    """Count screenshots-per-day matching ``fts_query`` over the window.

    Returns a sparse ``{iso_date: count}`` map — callers fill in the
    missing days. ``fts_query`` is the *already sanitised* MATCH phrase.
    """
    # ``modifier`` is a server-built constant of the form ``"-29 days"``
    # — only the clamped integer flows into it, never user-controlled
    # text. SQLite cannot bind a modifier to ``DATE(..., ?)`` directly,
    # which is why we splice it in here rather than parameterising it.
    modifier = f"-{days - 1} days"
    counts: dict[str, int] = {}
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT DATE(s.captured_at) AS day, COUNT(*) AS n
            FROM screenshots_fts
            JOIN screenshots s ON s.id = screenshots_fts.rowid
            WHERE screenshots_fts MATCH ?
              AND s.captured_at IS NOT NULL
              AND DATE(s.captured_at) >= DATE('now', ?)
            GROUP BY day
            ORDER BY day
            """,
            (fts_query, modifier),
        )
        rows = await cursor.fetchall()
    for row in rows:
        raw_day = row["day"]
        if raw_day is None:
            continue
        day = str(raw_day)
        try:
            datetime.strptime(day, _ISO_DATE_FMT)
        except ValueError:
            log.warning("sparkline_svg.bad_day_skipped", day=day)
            continue
        counts[day] = counts.get(day, 0) + int(row["n"])
    return counts


def _dense_entries(counts: dict[str, int], days: int) -> list[_DayCount]:
    """Project sparse ``counts`` onto a dense ``days``-entry window."""
    return [
        _DayCount(date=iso, count=counts.get(iso, 0))
        for iso in _dense_window(days)
    ]


def _polyline_points(entries: list[_DayCount]) -> tuple[str, str, int]:
    """Compute the ``polyline``, ``polygon`` (band) and ``peak`` strings.

    Returns ``(polyline, band, peak)`` — all empty strings (and ``peak``
    of ``0``) when ``entries`` is empty.
    """
    if not entries:
        return "", "", 0

    peak = max((entry["count"] for entry in entries), default=0)
    plot_w = _SVG_WIDTH - 2 * _PAD_X
    plot_h = _SVG_HEIGHT - 2 * _PAD_Y

    n = len(entries)
    step = 0.0 if n == 1 else plot_w / (n - 1)

    coords: list[tuple[float, float]] = []
    for index, entry in enumerate(entries):
        x = _PAD_X + (step * index if n > 1 else plot_w / 2)
        if peak > 0:
            ratio = entry["count"] / peak
            y = _BASELINE_Y - ratio * plot_h
        else:
            y = float(_BASELINE_Y)
        coords.append((x, y))

    polyline = " ".join(f"{x:.2f},{y:.2f}" for x, y in coords)
    first_x = coords[0][0]
    last_x = coords[-1][0]
    band = (
        f"{first_x:.2f},{_BASELINE_Y} "
        f"{polyline} "
        f"{last_x:.2f},{_BASELINE_Y}"
    )
    return polyline, band, peak


def _xml_escape(value: str) -> str:
    """Minimal XML attribute / text escape — no ``html`` import needed."""
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _render_svg(
    *,
    query: str,
    days: int,
    entries: list[_DayCount],
    polyline: str,
    band: str,
    peak: int,
    total: int,
) -> str:
    """Build the SVG document as a single ``str``.

    The output is deterministic given the inputs — no timestamps, no
    randomness — which keeps the upstream cache key stable.
    """
    title = (
        f"{_xml_escape(query) or 'empty query'} — {total} shot"
        f"{'' if total == 1 else 's'} over {days} day"
        f"{'' if days == 1 else 's'} (peak {peak})"
    )
    pieces: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{_SVG_WIDTH}" '
        f'height="{_SVG_HEIGHT}" viewBox="0 0 {_SVG_WIDTH} {_SVG_HEIGHT}" '
        f'role="img" aria-label="{_xml_escape(query)} sparkline">',
        f"<title>{title}</title>",
        # Baseline so an all-zero window still renders something visible.
        f'<line x1="{_PAD_X}" y1="{_BASELINE_Y}" '
        f'x2="{_SVG_WIDTH - _PAD_X}" y2="{_BASELINE_Y}" '
        f'stroke="{_BASELINE_STROKE}" stroke-width="1" '
        'stroke-dasharray="2,2" />',
    ]
    if band and peak > 0:
        pieces.append(
            f'<polygon points="{band}" fill="{_FILL_BAND}" stroke="none" />'
        )
    if polyline:
        pieces.append(
            f'<polyline points="{polyline}" fill="none" '
            f'stroke="{_STROKE}" stroke-width="1.5" '
            'stroke-linejoin="round" stroke-linecap="round" />'
        )
    pieces.append("</svg>")
    return "".join(pieces)


@router.get("/api/sparkline.svg")
async def sparkline_svg(
    q: str = Query(default="", description="FTS query"),
    days: int = Query(
        default=_DEFAULT_DAYS,
        ge=_MIN_DAYS,
        le=_MAX_DAYS,
        description="Trailing window length in days (1..365).",
    ),
) -> Response:
    """Return a 320x60 pure-SVG sparkline of shot counts per day."""
    window = _clamp_days(days)
    fts_query = _sanitise_fts(q)

    if fts_query:
        counts = await _fetch_counts(fts_query, window)
    else:
        counts = {}

    entries = _dense_entries(counts, window)
    polyline, band, peak = _polyline_points(entries)
    total = sum(entry["count"] for entry in entries)

    log.info(
        "sparkline_svg.rendered",
        query=q,
        sanitised=fts_query,
        days=window,
        total=total,
        peak=peak,
        non_zero_days=sum(1 for entry in entries if entry["count"] > 0),
    )

    svg = _render_svg(
        query=q,
        days=window,
        entries=entries,
        polyline=polyline,
        band=band,
        peak=peak,
        total=total,
    )
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={"Cache-Control": f"max-age={_CACHE_MAX_AGE}"},
    )


__all__ = ["router"]
