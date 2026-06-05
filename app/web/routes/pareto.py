"""Pareto chart of capture counts per app — ``/stats/pareto``.

Renders the combo chart (bars + cumulative line) defined by
:func:`app.pareto_stats.compute_app_pareto` and exposes the same data
as JSON at ``/api/stats/pareto.json`` for clients that want to render
their own visualisation.

The SVG layout is intentionally pure server-side: the bars and the
cumulative-share polyline are projected into pixel coordinates here so
the template can render with no Tailwind/Alpine/JS dependency beyond
what ``base.html`` already pulls in. This keeps the page printable,
copy-pasteable as a screenshot, and resilient to a JS-disabled browser
— which matters because the page is one of the few places where the
operator wants to *export* a chart, not just look at it.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.pareto_stats import compute_app_pareto
from app.web.templates_engine import templates

router = APIRouter(tags=["stats"])
log = get_logger("persona.pareto_stats.route")

# Window bounds match the conventions used by /stats/sentiment and the
# rest of the stats family — 1..365 days, defaulting to 30.
_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 365
_DEFAULT_DAYS: Final[int] = 30

# SVG geometry. A wider footprint than the sentiment chart because the
# bar count grows with the long tail (we cap visible bars at
# ``_MAX_VISIBLE_BARS`` so the strip stays readable on a laptop screen
# even when the operator has 200+ distinct app_names).
_SVG_WIDTH: Final[int] = 720
_SVG_HEIGHT: Final[int] = 320
_PAD_LEFT: Final[int] = 48
_PAD_RIGHT: Final[int] = 56
_PAD_TOP: Final[int] = 24
_PAD_BOTTOM: Final[int] = 64

# Visible-bar cap. Beyond ~40 bars the labels collide and the cumulative
# curve flattens to a single value; the table below the chart is the
# better surface for the very long tail. ``threshold_index`` always
# stays in the visible window because by definition the 80% mark is
# well to the left of the long tail.
_MAX_VISIBLE_BARS: Final[int] = 40

# Colours — emerald bars for the per-app share (matches the rest of the
# stats panels), accent purple for the cumulative line so the two
# series read distinctly even in grayscale mode.
_BAR_FILL: Final[str] = "#34d399"  # emerald-400
_LINE_STROKE: Final[str] = "#a78bfa"  # accent-400
_THRESHOLD_LINE: Final[str] = "#f59e0b"  # amber-500
_AXIS_COLOUR: Final[str] = "#52525b"
_AXIS_TEXT: Final[str] = "#a1a1aa"
_GRID_COLOUR: Final[str] = "#27272a"

_PARETO_THRESHOLD_VALUE: Final[float] = 80.0


def _build_chart(report: dict[str, Any]) -> dict[str, Any]:
    """Project the Pareto report into SVG coordinates for the template.

    Splits the work between Python (deterministic, testable, easy to
    reason about) and the Jinja layer (which only iterates over the
    prepared lists and emits ``<rect>`` / ``<polyline>`` / ``<text>``
    nodes). This is the same split used by
    :mod:`app.web.routes.sentiment_stats`.
    """
    apps: list[dict[str, Any]] = list(report["apps"])
    visible = apps[:_MAX_VISIBLE_BARS]
    visible_count = len(visible)

    plot_left = _PAD_LEFT
    plot_right = _SVG_WIDTH - _PAD_RIGHT
    plot_top = _PAD_TOP
    plot_bottom = _SVG_HEIGHT - _PAD_BOTTOM
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    # ``percent_individual`` for the very first (tallest) bar drives the
    # left y-axis ceiling; round up to the next 10 so the axis label
    # reads cleanly. Falls back to 100 when there is no data so the
    # chart still renders an empty grid rather than collapsing.
    if visible:
        top_share = max(item["percent_individual"] for item in visible)
        y_ceiling = max(10.0, _round_up_to_ten(top_share))
    else:
        y_ceiling = 100.0

    bars: list[dict[str, Any]] = []
    line_points: list[tuple[float, float]] = []

    if visible_count > 0:
        # Bars sit side by side; small inset on each side so the strokes
        # don't touch. The cumulative line dot for bar ``i`` lands at the
        # bar's horizontal centre so the eye reads the two series as
        # locked together.
        slot_width = plot_width / visible_count
        bar_inset = slot_width * 0.12
        for idx, item in enumerate(visible):
            slot_x = plot_left + idx * slot_width
            bar_x = slot_x + bar_inset
            bar_w = slot_width - 2 * bar_inset
            bar_h = (item["percent_individual"] / y_ceiling) * plot_height
            bar_y = plot_bottom - bar_h
            cumulative_y = plot_bottom - (
                item["percent_cumulative"] / 100.0
            ) * plot_height
            line_points.append((slot_x + slot_width / 2.0, cumulative_y))
            bars.append(
                {
                    "x": bar_x,
                    "y": bar_y,
                    "width": bar_w,
                    "height": bar_h,
                    "app": item["app"],
                    "shots": item["shots"],
                    "percent_individual": item["percent_individual"],
                    "percent_cumulative": item["percent_cumulative"],
                    "label_x": slot_x + slot_width / 2.0,
                    "is_threshold": idx == report["threshold_index"],
                }
            )

    polyline = " ".join(f"{x:.2f},{y:.2f}" for x, y in line_points)

    # Horizontal dashed line at 80% maps onto the *cumulative* axis,
    # which spans 0..100 (the right-hand axis). We project against the
    # full plot_height because the right axis ceiling is always 100.
    threshold_y = plot_bottom - (_PARETO_THRESHOLD_VALUE / 100.0) * plot_height

    # Vertical line at the threshold index — sits at the right edge of
    # the threshold bar's slot so the visual reads "everything up to
    # *and including* this bar accounts for >=80%".
    threshold_x: float | None = None
    threshold_idx = int(report["threshold_index"])
    if 0 <= threshold_idx < visible_count:
        slot_width = plot_width / visible_count
        threshold_x = plot_left + (threshold_idx + 1) * slot_width

    # Y-axis ticks for the individual-share axis (left). Five ticks
    # spaced from 0 to ``y_ceiling`` keeps the grid uncluttered.
    y_left_ticks: list[dict[str, Any]] = []
    for step in range(5 + 1):
        raw = y_ceiling * step / 5.0
        ty = plot_bottom - (raw / y_ceiling) * plot_height
        y_left_ticks.append({"y": ty, "label": f"{raw:.0f}%"})

    # Y-axis ticks for the cumulative axis (right). Always 0..100.
    y_right_ticks: list[dict[str, Any]] = []
    for raw in (0, 20, 40, 60, 80, 100):
        ty = plot_bottom - (raw / 100.0) * plot_height
        y_right_ticks.append({"y": ty, "label": f"{raw}%"})

    return {
        "width": _SVG_WIDTH,
        "height": _SVG_HEIGHT,
        "plot_left": plot_left,
        "plot_right": plot_right,
        "plot_top": plot_top,
        "plot_bottom": plot_bottom,
        "bars": bars,
        "polyline": polyline,
        "threshold_y": threshold_y,
        "threshold_x": threshold_x,
        "y_left_ticks": y_left_ticks,
        "y_right_ticks": y_right_ticks,
        "y_ceiling": y_ceiling,
        "visible_count": visible_count,
        "hidden_count": max(0, len(apps) - visible_count),
        "bar_fill": _BAR_FILL,
        "line_stroke": _LINE_STROKE,
        "threshold_line": _THRESHOLD_LINE,
        "axis_colour": _AXIS_COLOUR,
        "axis_text": _AXIS_TEXT,
        "grid_colour": _GRID_COLOUR,
        "pareto_threshold": _PARETO_THRESHOLD_VALUE,
    }


def _round_up_to_ten(value: float) -> float:
    """Round ``value`` up to the next multiple of 10.

    Used to compute a clean ceiling for the left y-axis. ``0`` stays
    ``0``; everything else lands at ``ceil(value / 10) * 10`` expressed
    as a float so the downstream arithmetic stays consistent.
    """
    if value <= 0:
        return 0.0
    remainder = value % 10
    if remainder == 0:
        return float(value)
    return float(value + (10 - remainder))


@router.get("/stats/pareto", response_class=HTMLResponse)
async def pareto_page(
    request: Request,
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the 80/20 capture-by-app chart with table + summary."""
    report = await compute_app_pareto(days=days)
    chart = _build_chart(report)

    log.info(
        "pareto.page",
        days=days,
        total_apps=report["total_apps"],
        total_shots=report["total_shots"],
        threshold_index=report["threshold_index"],
        threshold_count=report["threshold_count"],
    )

    return templates.TemplateResponse(
        request,
        "pareto.html",
        {
            "title": "Pareto apps",
            "active_nav": "stats",
            "days": days,
            "min_days": _MIN_DAYS,
            "max_days": _MAX_DAYS,
            "report": report,
            "chart": chart,
        },
    )


@router.get("/api/stats/pareto.json", response_class=JSONResponse)
async def pareto_json(
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> JSONResponse:
    """Raw Pareto report as JSON for third-party visualisers."""
    report = await compute_app_pareto(days=days)
    return JSONResponse(report)
