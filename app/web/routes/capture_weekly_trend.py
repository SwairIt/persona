"""Weekly capture-trend chart — HTML page (SVG bars) + JSON endpoint.

v1.9 feature 2/3. Reads :func:`app.capture_weekly_trend.weekly_counts`
and renders the per-ISO-week total screenshot count as a pure SVG bar
chart. No JS, no canvas, no client-side chart library — bar geometry is
pre-computed server-side so the page is a single round-trip and the
data is just as readable in "View Source".

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main`; the task spec forbids touching ``main.py``.
Wire it up in a follow-up patch with::

    from app.web.routes import capture_weekly_trend as capture_weekly_trend_routes
    app.include_router(capture_weekly_trend_routes.router)
"""

from __future__ import annotations

from typing import Final, TypedDict

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.capture_weekly_trend import (
    WeeklyCaptureBucket,
    weekly_counts,
)
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.weekly_trend")

router = APIRouter(tags=["weekly-trend"])

# ---------------------------------------------------------------------------
# SVG geometry
# ---------------------------------------------------------------------------
#
# Wide enough to show 26 weekly bars comfortably (each bar gets ~32px of
# horizontal slot), short enough to fit above-the-fold. Matches the
# proportions of the retention-trend chart so the two pages feel
# consistent when navigated between.
_SVG_WIDTH: Final[int] = 960
_SVG_HEIGHT: Final[int] = 260
_PAD_LEFT: Final[int] = 52  # room for Y-axis labels
_PAD_RIGHT: Final[int] = 16
_PAD_TOP: Final[int] = 16
_PAD_BOTTOM: Final[int] = 40  # room for X-axis labels

# Bar palette. Single colour (no stacking on this chart) — accent blue
# reads well on both light and dark themes and lines up with the rest of
# Persona's stats pages.
_COLOR_BAR_FILL: Final[str] = "rgba(56, 189, 248, 0.55)"  # sky-400 @ 55%
_COLOR_BAR_STROKE: Final[str] = "#38bdf8"  # sky-400
_COLOR_BAR_PEAK_FILL: Final[str] = "rgba(244, 114, 182, 0.65)"  # pink-400 @ 65%
_COLOR_BAR_PEAK_STROKE: Final[str] = "#f472b6"  # pink-400
_COLOR_AXIS: Final[str] = "#3f3f46"
_COLOR_GRID: Final[str] = "#27272a"

_MIN_WEEKS: Final[int] = 1
_MAX_WEEKS: Final[int] = 520
_DEFAULT_WEEKS: Final[int] = 26

# Minimum gap (in px) between adjacent bars. The actual gap may grow when
# the window is small enough that bars would otherwise look fat and
# ridiculous; it never shrinks below this.
_MIN_BAR_GAP_PX: Final[float] = 2.0


class _Bar(TypedDict):
    """Pre-computed SVG geometry for a single weekly bar.

    Exposed to the template as a plain mapping; the strong typing here
    keeps the Python side honest under ``mypy --strict`` without forcing
    Jinja to learn about it.
    """

    x: float
    y: float
    width: float
    height: float
    week_start: str
    shots: int
    is_peak: bool


class _XAxisLabel(TypedDict):
    """One X-axis tick: centred under a bar, label trimmed to MM-DD."""

    x: float
    label: str


class _YAxisTick(TypedDict):
    """One Y-axis gridline + label, top-of-plot ``y`` in SVG space."""

    y: float
    label: str


def _clamp_window(weeks: int) -> int:
    """Clamp ``weeks`` to the safe UI range. Mirrors the model-side guard."""
    if weeks < _MIN_WEEKS:
        return _MIN_WEEKS
    if weeks > _MAX_WEEKS:
        return _MAX_WEEKS
    return weeks


def _bar_geometry(
    entries: list[WeeklyCaptureBucket],
) -> tuple[list[_Bar], int]:
    """Compute SVG geometry for each weekly bar.

    Returns ``(bars, peak)`` where ``bars`` is a list of dicts —
    ``{x, y, width, height, week_start, shots, is_peak}`` — and ``peak``
    is the highest weekly shot count across the window (``0`` when the
    whole window is empty).

    Geometry rules:

    * Bars are anchored to the X-axis baseline; the ``y`` returned is
      the top edge in SVG space (Y grows downward), ``height`` is the
      vertical extent.
    * Empty weeks still emit a bar entry with ``height = 0`` so the
      template can iterate without missing-week guards and the X-axis
      ticks have well-defined ``x`` anchors.
    * Bar width tracks the plot width / N, with a guaranteed minimum
      gap of :data:`_MIN_BAR_GAP_PX` between neighbours.
    """
    n = len(entries)
    plot_w = _SVG_WIDTH - _PAD_LEFT - _PAD_RIGHT
    plot_h = _SVG_HEIGHT - _PAD_TOP - _PAD_BOTTOM
    baseline_y = _SVG_HEIGHT - _PAD_BOTTOM

    if n == 0:
        return [], 0

    peak = max((entry["shots"] for entry in entries), default=0)

    slot_w = plot_w / n
    bar_w = max(slot_w - _MIN_BAR_GAP_PX, 1.0)

    bars: list[_Bar] = []
    for index, entry in enumerate(entries):
        # Centre each bar inside its slot so the gap is split evenly
        # between neighbours.
        slot_x = _PAD_LEFT + slot_w * index
        x = slot_x + (slot_w - bar_w) / 2

        shots = entry["shots"]
        height = (shots / peak) * plot_h if peak > 0 else 0.0
        y = baseline_y - height

        bars.append(
            _Bar(
                x=x,
                y=y,
                width=bar_w,
                height=height,
                week_start=entry["week_start"],
                shots=shots,
                is_peak=shots == peak and peak > 0,
            )
        )

    return bars, peak


def _x_axis_labels(
    bars: list[_Bar],
) -> list[_XAxisLabel]:
    """Pick a handful of evenly-spaced X-axis tick labels.

    A 26-week window with one label per bar would render readably, but
    on wider windows (e.g. 104 weeks) the axis would smear; we cap at
    eight evenly-distributed labels so the axis stays legible at any
    zoom level.
    """
    n = len(bars)
    if n == 0:
        return []
    target_ticks = 8
    if n <= target_ticks:
        indices = list(range(n))
    else:
        step = (n - 1) / (target_ticks - 1)
        indices = [round(i * step) for i in range(target_ticks)]

    seen: set[int] = set()
    out: list[_XAxisLabel] = []
    for idx in indices:
        if idx in seen:
            continue
        seen.add(idx)
        bar = bars[idx]
        # Centre tick under the bar.
        tick_x = bar["x"] + bar["width"] / 2.0
        # Trim to MM-DD; the full week_start sits in the table below.
        full_label = bar["week_start"]
        out.append(_XAxisLabel(x=tick_x, label=full_label[5:]))
    return out


def _y_axis_ticks(peak: int) -> list[_YAxisTick]:
    """Five evenly-spaced gridlines from baseline up to peak.

    An empty window still emits a single baseline tick so the chart
    isn't an unmarked rectangle.
    """
    baseline_y: float = float(_SVG_HEIGHT - _PAD_BOTTOM)
    plot_h: float = float(_SVG_HEIGHT - _PAD_TOP - _PAD_BOTTOM)
    if peak <= 0:
        return [_YAxisTick(y=baseline_y, label="0")]
    target_ticks = 4
    out: list[_YAxisTick] = [_YAxisTick(y=baseline_y, label="0")]
    for i in range(1, target_ticks + 1):
        ratio = i / target_ticks
        value = round(peak * ratio)
        y = baseline_y - ratio * plot_h
        out.append(_YAxisTick(y=y, label=str(value)))
    return out


@router.get("/stats/weekly-trend", response_class=HTMLResponse)
async def weekly_trend_page(
    request: Request,
    weeks: int = Query(default=_DEFAULT_WEEKS, ge=_MIN_WEEKS, le=_MAX_WEEKS),
) -> HTMLResponse:
    """Render the weekly capture-trend page (pure SVG bar chart)."""
    window = _clamp_window(weeks)
    entries = await weekly_counts(weeks=window)
    bars, peak = _bar_geometry(entries)

    total_shots = sum(e["shots"] for e in entries)
    non_zero_weeks = sum(1 for e in entries if e["shots"] > 0)
    average_per_week = (total_shots / window) if window else 0.0

    return templates.TemplateResponse(
        request,
        "capture_weekly_trend.html",
        {
            "title": "Weekly capture trend",
            "active_nav": "stats",
            "weeks": window,
            "entries": entries,
            "bars": bars,
            "peak": peak,
            "total_shots": total_shots,
            "non_zero_weeks": non_zero_weeks,
            "average_per_week": average_per_week,
            "svg_width": _SVG_WIDTH,
            "svg_height": _SVG_HEIGHT,
            "baseline_y": _SVG_HEIGHT - _PAD_BOTTOM,
            "pad_left": _PAD_LEFT,
            "pad_right": _PAD_RIGHT,
            "pad_top": _PAD_TOP,
            "pad_bottom": _PAD_BOTTOM,
            "x_axis_labels": _x_axis_labels(bars),
            "y_axis_ticks": _y_axis_ticks(peak),
            "color_bar_fill": _COLOR_BAR_FILL,
            "color_bar_stroke": _COLOR_BAR_STROKE,
            "color_bar_peak_fill": _COLOR_BAR_PEAK_FILL,
            "color_bar_peak_stroke": _COLOR_BAR_PEAK_STROKE,
            "color_axis": _COLOR_AXIS,
            "color_grid": _COLOR_GRID,
            "json_url": f"/api/weekly-trend.json?weeks={window}",
        },
    )


@router.get("/api/weekly-trend.json", response_class=JSONResponse)
async def weekly_trend_json(
    weeks: int = Query(default=_DEFAULT_WEEKS, ge=_MIN_WEEKS, le=_MAX_WEEKS),
) -> JSONResponse:
    """Machine-readable counterpart to :func:`weekly_trend_page`."""
    window = _clamp_window(weeks)
    entries = await weekly_counts(weeks=window)
    total_shots = sum(e["shots"] for e in entries)
    peak = max((e["shots"] for e in entries), default=0)
    non_zero_weeks = sum(1 for e in entries if e["shots"] > 0)
    return JSONResponse(
        {
            "weeks": window,
            "total_shots": total_shots,
            "peak_week_shots": peak,
            "non_zero_weeks": non_zero_weeks,
            "average_per_week": (total_shots / window) if window else 0.0,
            "entries": [dict(entry) for entry in entries],
        }
    )


__all__ = ["router"]
