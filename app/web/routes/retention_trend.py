"""Retention-activity trend — HTML page with SVG stacked area + JSON endpoint.

v0.88 feature 3/3. Reads :func:`app.retention_trend.daily_retention_stats`
and renders the per-day count of demoted-to-warm, demoted-to-cold and
hard-deleted screenshots as a pure SVG stacked area chart. No JS, no
canvas, no client-side chart library — the polygons and polylines are
pre-computed server-side so the page is a single round-trip and the
data is just as readable in "View Source".

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main`; the task spec forbids touching ``main.py``.
Wire it up in a follow-up patch with::

    from app.web.routes import retention_trend as retention_trend_routes
    app.include_router(retention_trend_routes.router)
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.retention_trend import (
    RetentionTrendEntry,
    daily_retention_stats,
)
from app.web.templates_engine import templates

log = get_logger("persona.retention.trend")

router = APIRouter(tags=["retention-trend"])

# ---------------------------------------------------------------------------
# SVG geometry
# ---------------------------------------------------------------------------
#
# Wide enough to show 60 daily ticks comfortably; short enough to fit
# above-the-fold on a 1080p screen. Matches the proportions of the
# tag-trend sparkline elsewhere in the app, just bigger.
_SVG_WIDTH: Final[int] = 960
_SVG_HEIGHT: Final[int] = 240
_PAD_LEFT: Final[int] = 48  # room for Y-axis labels
_PAD_RIGHT: Final[int] = 16
_PAD_TOP: Final[int] = 16
_PAD_BOTTOM: Final[int] = 32  # room for X-axis labels

# Stacked-area palette. Picked to read distinctly in both light and dark
# themes, and to keep the visual story honest: warm-demotions sit on the
# bottom (most frequent, least destructive), cold-demotions in the
# middle, hard-deletes on top (rarest, most destructive — and the band
# the user most wants to spot).
_COLOR_WARM_STROKE: Final[str] = "#f59e0b"  # amber-500
_COLOR_WARM_FILL: Final[str] = "rgba(245, 158, 11, 0.30)"
_COLOR_COLD_STROKE: Final[str] = "#38bdf8"  # sky-400
_COLOR_COLD_FILL: Final[str] = "rgba(56, 189, 248, 0.30)"
_COLOR_DELETE_STROKE: Final[str] = "#f43f5e"  # rose-500
_COLOR_DELETE_FILL: Final[str] = "rgba(244, 63, 94, 0.30)"
_COLOR_AXIS: Final[str] = "#3f3f46"
_COLOR_GRID: Final[str] = "#27272a"

_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 3650
_DEFAULT_DAYS: Final[int] = 60


def _clamp_window(days: int) -> int:
    """Clamp ``days`` to the safe UI range. Mirrors the model-side guard."""
    if days < _MIN_DAYS:
        return _MIN_DAYS
    if days > _MAX_DAYS:
        return _MAX_DAYS
    return days


def _stacked_layers(
    entries: list[RetentionTrendEntry],
) -> tuple[
    list[dict[str, float]],
    list[dict[str, float]],
    list[dict[str, float]],
    int,
]:
    """Compute SVG ``(x, y)`` coordinates for the three stacked layers.

    Returns four values:

    * ``warm`` — list of ``{x, y, base, top}`` per day for the
      demoted-warm layer (the bottom of the stack);
    * ``cold`` — same for the demoted-cold layer (middle);
    * ``deleted`` — same for the hard-deleted layer (top);
    * ``peak`` — peak stacked total across the window (``0`` when empty).

    ``base`` is the Y coordinate of the layer's bottom edge,
    ``top`` is the Y coordinate of its top edge. Both are in SVG space
    (Y grows downward), so the polygons can be built as a single loop
    in the template.
    """
    n = len(entries)
    plot_w = _SVG_WIDTH - _PAD_LEFT - _PAD_RIGHT
    plot_h = _SVG_HEIGHT - _PAD_TOP - _PAD_BOTTOM
    baseline_y = _SVG_HEIGHT - _PAD_BOTTOM

    if n == 0:
        return [], [], [], 0

    # Peak stacked total drives the Y-axis. ``max`` over an empty
    # sequence would explode, hence ``default=0``.
    peak = max(
        (
            entry["demoted_warm"] + entry["demoted_cold"] + entry["hard_deleted"]
            for entry in entries
        ),
        default=0,
    )

    # Spread points evenly across the plot width. Single-entry windows
    # pin the point to the centre so the chart still renders.
    step = 0.0 if n == 1 else plot_w / (n - 1)

    warm_points: list[dict[str, float]] = []
    cold_points: list[dict[str, float]] = []
    deleted_points: list[dict[str, float]] = []

    for index, entry in enumerate(entries):
        x = _PAD_LEFT + (step * index if n > 1 else plot_w / 2)

        warm_n = entry["demoted_warm"]
        cold_n = entry["demoted_cold"]
        deleted_n = entry["hard_deleted"]

        if peak > 0:
            scale = plot_h / peak
            warm_h = warm_n * scale
            cold_h = cold_n * scale
            deleted_h = deleted_n * scale
        else:
            warm_h = cold_h = deleted_h = 0.0

        warm_base = baseline_y
        warm_top = baseline_y - warm_h
        cold_base = warm_top
        cold_top = cold_base - cold_h
        deleted_base = cold_top
        deleted_top = deleted_base - deleted_h

        warm_points.append({"x": x, "base": warm_base, "top": warm_top})
        cold_points.append({"x": x, "base": cold_base, "top": cold_top})
        deleted_points.append({"x": x, "base": deleted_base, "top": deleted_top})

    return warm_points, cold_points, deleted_points, peak


def _polygon_attr(layer: list[dict[str, float]]) -> str:
    """Build the ``points="..."`` string for one stacked-area layer.

    The polygon is constructed by walking the layer's ``top`` edge left
    to right, then walking the ``base`` edge right to left so the path
    closes cleanly without an extra ``Z``.
    """
    if not layer:
        return ""
    top_walk = " ".join(f"{p['x']:.2f},{p['top']:.2f}" for p in layer)
    base_walk = " ".join(f"{p['x']:.2f},{p['base']:.2f}" for p in reversed(layer))
    return f"{top_walk} {base_walk}"


def _polyline_attr(layer: list[dict[str, float]]) -> str:
    """Build the ``points="..."`` string tracing the top edge of one layer.

    Drawn on top of the polygon so the layer's top line reads crisply
    against neighbouring fills.
    """
    if not layer:
        return ""
    return " ".join(f"{p['x']:.2f},{p['top']:.2f}" for p in layer)


def _x_axis_labels(
    entries: list[RetentionTrendEntry],
    points: list[dict[str, float]],
) -> list[dict[str, object]]:
    """Pick a handful of evenly-spaced X-axis tick labels.

    A 60-day window with one label per day would render as a black
    smear; we cap at six labels evenly distributed across the window so
    the axis stays readable at typical zoom levels.
    """
    if not entries or not points:
        return []
    target_ticks = 6
    n = len(entries)
    if n <= target_ticks:
        indices = list(range(n))
    else:
        step = (n - 1) / (target_ticks - 1)
        indices = [round(i * step) for i in range(target_ticks)]
    seen: set[int] = set()
    out: list[dict[str, object]] = []
    for idx in indices:
        if idx in seen:
            continue
        seen.add(idx)
        out.append(
            {
                "x": points[idx]["x"],
                # Trim to MM-DD so the label fits comfortably under a
                # tick mark. The full date is still in the table below.
                "label": entries[idx]["date"][5:],
            }
        )
    return out


def _y_axis_ticks(peak: int) -> list[dict[str, object]]:
    """Five evenly-spaced gridlines from baseline up to peak.

    A ``peak`` of 0 still emits a single baseline tick so the empty
    chart has at least one labelled gridline instead of an unmarked
    rectangle.
    """
    baseline_y = _SVG_HEIGHT - _PAD_BOTTOM
    plot_h = _SVG_HEIGHT - _PAD_TOP - _PAD_BOTTOM
    if peak <= 0:
        return [{"y": baseline_y, "label": "0"}]
    target_ticks = 4
    out: list[dict[str, object]] = [{"y": baseline_y, "label": "0"}]
    for i in range(1, target_ticks + 1):
        ratio = i / target_ticks
        value = round(peak * ratio)
        y = baseline_y - ratio * plot_h
        out.append({"y": y, "label": str(value)})
    return out


@router.get("/stats/retention-trend", response_class=HTMLResponse)
async def retention_trend_page(
    request: Request,
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the retention-activity trend page (pure SVG stacked area)."""
    window = _clamp_window(days)
    entries = await daily_retention_stats(days=window)
    warm_layer, cold_layer, deleted_layer, peak = _stacked_layers(entries)

    total_warm = sum(e["demoted_warm"] for e in entries)
    total_cold = sum(e["demoted_cold"] for e in entries)
    total_delete = sum(e["hard_deleted"] for e in entries)

    return templates.TemplateResponse(
        request,
        "retention_trend.html",
        {
            "title": "Retention trend",
            "active_nav": "stats",
            "days": window,
            "entries": entries,
            "total_demoted_warm": total_warm,
            "total_demoted_cold": total_cold,
            "total_hard_deleted": total_delete,
            "peak": peak,
            "svg_width": _SVG_WIDTH,
            "svg_height": _SVG_HEIGHT,
            "baseline_y": _SVG_HEIGHT - _PAD_BOTTOM,
            "pad_left": _PAD_LEFT,
            "pad_right": _PAD_RIGHT,
            "pad_top": _PAD_TOP,
            "pad_bottom": _PAD_BOTTOM,
            "warm_polygon": _polygon_attr(warm_layer),
            "cold_polygon": _polygon_attr(cold_layer),
            "deleted_polygon": _polygon_attr(deleted_layer),
            "warm_polyline": _polyline_attr(warm_layer),
            "cold_polyline": _polyline_attr(cold_layer),
            "deleted_polyline": _polyline_attr(deleted_layer),
            "x_axis_labels": _x_axis_labels(entries, warm_layer),
            "y_axis_ticks": _y_axis_ticks(peak),
            "color_warm_stroke": _COLOR_WARM_STROKE,
            "color_warm_fill": _COLOR_WARM_FILL,
            "color_cold_stroke": _COLOR_COLD_STROKE,
            "color_cold_fill": _COLOR_COLD_FILL,
            "color_delete_stroke": _COLOR_DELETE_STROKE,
            "color_delete_fill": _COLOR_DELETE_FILL,
            "color_axis": _COLOR_AXIS,
            "color_grid": _COLOR_GRID,
            "json_url": f"/api/retention-trend.json?days={window}",
        },
    )


@router.get("/api/retention-trend.json", response_class=JSONResponse)
async def retention_trend_json(
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> JSONResponse:
    """Machine-readable counterpart to :func:`retention_trend_page`."""
    window = _clamp_window(days)
    entries = await daily_retention_stats(days=window)
    total_warm = sum(e["demoted_warm"] for e in entries)
    total_cold = sum(e["demoted_cold"] for e in entries)
    total_delete = sum(e["hard_deleted"] for e in entries)
    peak = max(
        (e["demoted_warm"] + e["demoted_cold"] + e["hard_deleted"] for e in entries),
        default=0,
    )
    return JSONResponse(
        {
            "days": window,
            "totals": {
                "demoted_warm": total_warm,
                "demoted_cold": total_cold,
                "hard_deleted": total_delete,
            },
            "peak_day_total": peak,
            "entries": [dict(entry) for entry in entries],
        }
    )


__all__ = ["router"]
