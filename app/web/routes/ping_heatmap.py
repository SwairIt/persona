"""Hour-of-day x day-of-month heatmap for external ``ping`` rows (v0.75).

The :mod:`external_ping` table (added in v0.74) collects best-effort
heartbeats from external scripts — CLI focus timers, CI watchers,
meeting bots, etc.  A single recent-pings table is enough to spot
individual rows but says nothing about the **shape** of activity across
days.

This module turns the last 30 days of ``external_ping.ts`` into a dense
30-column (days, oldest → newest) by 24-row (hour-of-day, 0 at the top)
grid and renders it as a pure-SVG heatmap.  Cell colour-intensity is a
discrete ``0..4`` level derived from the non-zero cell distribution so a
mostly-quiet month doesn't make the few busy cells look identical to
the empty ones.

Endpoints
---------
* ``GET /stats/ping-heatmap``         — Tailwind page wrapping the SVG.
* ``GET /api/ping-heatmap.json``      — same data as a JSON matrix.

Design notes
------------
* All SQL is parametrised — the ``-30 days`` modifier is the only
  string-formatted value and it is constructed from a clamped integer.
* The grid is built server-side so the template stays declarative and
  no JavaScript is needed to render the chart.
* Levels are bucketed by percentile cut-offs across the *non-zero* cell
  distribution.  Any positive count is at least level 1 even when the
  distribution is flat enough that the percentile thresholds would
  otherwise swallow it (mirrors the convention used by
  :mod:`app.heatmap`).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TypedDict

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.ping_heatmap")

router = APIRouter(tags=["ping_heatmap"])

# Window — kept compact so the SVG stays readable on a phone in
# portrait. 30 days * 24 hours = 720 cells.
_DEFAULT_DAYS = 30
_MIN_DAYS = 1
# Hard upper bound matches the screenshot-side heatmap so an operator
# can't blow up the page render with a ``?days=99999`` query string.
_MAX_DAYS = 365

_HOURS_PER_DAY = 24

# Discrete intensity palette — same emerald scale as
# :mod:`app.web.routes.heatmap` so the two stats pages feel like one
# family. Indexed by level 0..4.
_LEVEL_FILLS: tuple[str, ...] = (
    "#27272a",  # zinc-800 — empty
    "#064e3b",  # emerald-900 — quiet
    "#047857",  # emerald-700 — modest
    "#10b981",  # emerald-500 — busy
    "#34d399",  # emerald-400 — peak
)

# SVG geometry — pure numbers, no JS / no animation deps. Tiles are
# slightly taller than wide because there are far more rows (24) than
# columns (30), so square tiles would force horizontal scroll on most
# phones; keeping ``_TILE_W`` modest preserves a single-screen render.
_TILE_W = 14
_TILE_H = 12
_GAP = 2
_LEFT_PAD = 30  # room for hour labels on the Y axis
_TOP_PAD = 22  # room for day-of-month labels on the X axis


class PingCell(TypedDict):
    """One ``(day, hour)`` bucket of the heatmap."""

    date: str
    hour: int
    count: int
    level: int


class PingHeatmapPayload(TypedDict):
    """The full matrix + meta returned by :func:`_compute_matrix`."""

    start_date: str
    end_date: str
    days: int
    hours: int
    total: int
    max_count: int
    cells: list[list[PingCell]]


def _clamp_days(value: int) -> int:
    """Clamp ``value`` into ``[_MIN_DAYS, _MAX_DAYS]``."""
    if value < _MIN_DAYS:
        return _MIN_DAYS
    if value > _MAX_DAYS:
        return _MAX_DAYS
    return value


def _percentile(sorted_values: list[int], pct: float) -> float:
    """Linear-interpolated percentile across a *sorted* ascending list.

    ``pct`` is in ``[0, 1]``. Empty input returns ``0.0`` so callers can
    safely use the result as a numeric threshold without a guard.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = pct * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def _level_for(count: int, p33: float, p66: float, p90: float) -> int:
    """Bucket a raw cell count into a discrete ``0..4`` intensity level.

    Empty cells are always level 0; positive cells start at level 1
    even if the percentile cut-offs would otherwise collapse the bottom
    band to zero.
    """
    if count <= 0:
        return 0
    if count > p90:
        return 4
    if count > p66:
        return 3
    if count > p33:
        return 2
    return 1


async def _compute_matrix(days: int = _DEFAULT_DAYS) -> PingHeatmapPayload:
    """Aggregate ``external_ping.ts`` into a dense ``days x 24`` matrix.

    The returned matrix is column-major: ``cells[day_index][hour]`` —
    ``day_index = 0`` is the oldest day in the window, ``day_index =
    days - 1`` is today (anchored to UTC, matching the SQL
    ``datetime('now')`` default the ``ts`` column uses).
    """
    window = _clamp_days(days)
    modifier = f"-{window - 1} days"

    anchor = datetime.now(UTC).date()
    start = anchor - timedelta(days=window - 1)
    start_iso = start.isoformat()
    end_iso = anchor.isoformat()

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT DATE(ts) AS day, "
            "CAST(strftime('%H', ts) AS INTEGER) AS hr, "
            "COUNT(*) AS n "
            "FROM external_ping "
            "WHERE ts IS NOT NULL "
            "AND DATE(ts) >= DATE('now', ?) "
            "GROUP BY day, hr",
            (modifier,),
        )
        raw_rows = await cursor.fetchall()

    counts: dict[tuple[date, int], int] = {}
    for row in raw_rows:
        raw_day = row["day"]
        raw_hour = row["hr"]
        if not raw_day or raw_hour is None:
            continue
        try:
            day = date.fromisoformat(str(raw_day)[:10])
        except ValueError:
            log.warning("ping_heatmap.bad_day_skipped", day=str(raw_day))
            continue
        try:
            hour = int(raw_hour)
        except (TypeError, ValueError):
            log.warning("ping_heatmap.bad_hour_skipped", hour=str(raw_hour))
            continue
        if not 0 <= hour < _HOURS_PER_DAY:
            log.warning("ping_heatmap.out_of_range_skipped", hour=hour)
            continue
        key = (day, hour)
        counts[key] = counts.get(key, 0) + int(row["n"])

    non_zero_sorted: list[int] = sorted(v for v in counts.values() if v > 0)
    p33 = _percentile(non_zero_sorted, 0.33)
    p66 = _percentile(non_zero_sorted, 0.66)
    p90 = _percentile(non_zero_sorted, 0.90)

    cells: list[list[PingCell]] = []
    total = 0
    max_count = 0
    for day_offset in range(window):
        current = start + timedelta(days=day_offset)
        column: list[PingCell] = []
        for hour in range(_HOURS_PER_DAY):
            n = counts.get((current, hour), 0)
            max_count = max(max_count, n)
            total += n
            column.append(
                PingCell(
                    date=current.isoformat(),
                    hour=hour,
                    count=n,
                    level=_level_for(n, p33, p66, p90),
                )
            )
        cells.append(column)

    log.info(
        "ping_heatmap.computed",
        start=start_iso,
        end=end_iso,
        days=window,
        total=total,
        max_count=max_count,
        non_zero_cells=len(non_zero_sorted),
        p33=p33,
        p66=p66,
        p90=p90,
    )

    return PingHeatmapPayload(
        start_date=start_iso,
        end_date=end_iso,
        days=window,
        hours=_HOURS_PER_DAY,
        total=total,
        max_count=max_count,
        cells=cells,
    )


def _column_labels(payload: PingHeatmapPayload) -> list[dict[str, object]]:
    """Pick a sparse set of X-axis labels — one per ~5 columns.

    The chart has up to 30 columns; labelling every one crowds the
    header and labelling only the first/last leaves the middle bare.
    Stepping every 5 columns plus pinning the rightmost (today) keeps
    the axis legible without overlap.
    """
    columns = payload["cells"]
    if not columns:
        return []
    labels: list[dict[str, object]] = []
    step = max(1, len(columns) // 6)
    last_index = len(columns) - 1
    seen: set[int] = set()
    for index in range(0, len(columns), step):
        seen.add(index)
    seen.add(last_index)
    for index in sorted(seen):
        anchor = columns[index][0]
        try:
            day = date.fromisoformat(anchor["date"])
        except ValueError:
            continue
        x = _LEFT_PAD + index * (_TILE_W + _GAP) + _TILE_W / 2
        labels.append(
            {
                "col": index,
                "x": x,
                "label": f"{day.month:02d}-{day.day:02d}",
            }
        )
    return labels


def _row_labels() -> list[dict[str, object]]:
    """Hour labels for the Y-axis — every third hour to keep it sparse."""
    labels: list[dict[str, object]] = []
    for hour in range(_HOURS_PER_DAY):
        if hour % 3 != 0:
            continue
        y = _TOP_PAD + hour * (_TILE_H + _GAP) + _TILE_H - 2
        labels.append({"hour": hour, "y": y, "label": f"{hour:02d}"})
    return labels


def _busiest_cell(payload: PingHeatmapPayload) -> PingCell | None:
    """Return the single ``(day, hour)`` cell with the highest count."""
    best: PingCell | None = None
    for column in payload["cells"]:
        for cell in column:
            if cell["count"] <= 0:
                continue
            if best is None or cell["count"] > best["count"]:
                best = cell
    return best


@router.get("/stats/ping-heatmap", response_class=HTMLResponse)
async def ping_heatmap_page(
    request: Request,
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the SVG ping heatmap for the last ``days`` days."""
    payload = await _compute_matrix(days=days)
    window = payload["days"]

    svg_width = _LEFT_PAD + window * (_TILE_W + _GAP)
    svg_height = _TOP_PAD + _HOURS_PER_DAY * (_TILE_H + _GAP)

    column_labels = _column_labels(payload)
    row_labels = _row_labels()
    busiest = _busiest_cell(payload)

    return templates.TemplateResponse(
        request,
        "ping_heatmap.html",
        {
            "title": f"Ping heatmap · last {window} days",
            "active_nav": "stats",
            "days": window,
            "start_date": payload["start_date"],
            "end_date": payload["end_date"],
            "total": payload["total"],
            "max_count": payload["max_count"],
            "cells": payload["cells"],
            "level_fills": _LEVEL_FILLS,
            "tile_w": _TILE_W,
            "tile_h": _TILE_H,
            "gap": _GAP,
            "left_pad": _LEFT_PAD,
            "top_pad": _TOP_PAD,
            "svg_width": svg_width,
            "svg_height": svg_height,
            "column_labels": column_labels,
            "row_labels": row_labels,
            "busiest": busiest,
        },
    )


@router.get("/api/ping-heatmap.json", response_class=JSONResponse)
async def ping_heatmap_json(
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> JSONResponse:
    """Return the raw matrix + meta as JSON.

    Shape mirrors :class:`PingHeatmapPayload`: ``cells`` is a
    ``days``-long list of 24-entry hour columns, each entry a
    ``{date, hour, count, level}`` dict.
    """
    payload = await _compute_matrix(days=days)
    busiest = _busiest_cell(payload)
    return JSONResponse(
        {
            "start_date": payload["start_date"],
            "end_date": payload["end_date"],
            "days": payload["days"],
            "hours": payload["hours"],
            "total": payload["total"],
            "max_count": payload["max_count"],
            "busiest": dict(busiest) if busiest is not None else None,
            "cells": [[dict(cell) for cell in column] for column in payload["cells"]],
        }
    )
