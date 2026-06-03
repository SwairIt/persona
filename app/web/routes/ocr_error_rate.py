"""OCR error-rate panel — `/stats/ocr-error-rate` HTML page + JSON API.

Renders the daily error-rate timeseries produced by
:mod:`app.ocr.error_rate` as a 480x180 SVG line chart with red highlights
on days whose share of low-confidence-or-empty shots exceeded
:data:`_HIGHLIGHT_PCT`. A machine-readable counterpart lives at
``/api/ocr-error-rate.json``.

The chart layout (paddings, axis ticks, dot radii) is computed in this
module rather than the template so the HTML stays focused on markup and
the data shape is straightforward to test in isolation.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.ocr.error_rate import ErrorRateDay, error_rate_by_day
from app.web.templates_engine import templates

router = APIRouter(tags=["stats"])
log = get_logger("persona.ocr.error_rate")

# Mirrors :data:`app.ocr.error_rate._MAX_DAYS` — duplicated here so
# FastAPI's query-parameter validator returns a 422 before the data
# layer even starts the scan.
_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 365
_DEFAULT_DAYS: Final[int] = 30

# Quick-select buttons for the window switcher. 30 is the default and
# matches the data-layer default.
_WINDOW_CHOICES: Final[tuple[int, ...]] = (7, 14, 30, 60, 90)

# Days whose error rate exceeds this percentage are coloured red in the
# chart and tagged ``highlight`` in the JSON payload. 20% is the
# threshold the task spec calls out explicitly.
_HIGHLIGHT_PCT: Final[float] = 20.0

# SVG geometry. The chart is fixed at 480x180 per the spec; paddings are
# tuned so the y-axis labels fit without the polyline running into them.
_SVG_WIDTH: Final[int] = 480
_SVG_HEIGHT: Final[int] = 180
_PAD_LEFT: Final[int] = 36
_PAD_RIGHT: Final[int] = 10
_PAD_TOP: Final[int] = 14
_PAD_BOTTOM: Final[int] = 26

# Colours — match the existing dark-mode palette used by other stats
# pages (zinc + rose for "bad", accent purple for the main series).
_STROKE_NORMAL: Final[str] = "#a78bfa"  # accent-400
_FILL_BAND: Final[str] = "rgba(167, 139, 250, 0.10)"
_DOT_NORMAL: Final[str] = "#a78bfa"
_DOT_HIGHLIGHT: Final[str] = "#f43f5e"  # rose-500
_BASELINE_COLOUR: Final[str] = "#3f3f46"
_AXIS_COLOUR: Final[str] = "#52525b"
_AXIS_TEXT: Final[str] = "#71717a"
_HIGHLIGHT_LINE: Final[str] = "rgba(244, 63, 94, 0.35)"


def _y_axis_max(buckets: list[ErrorRateDay]) -> float:
    """Compute a sensible y-axis ceiling for the chart.

    The axis is anchored at zero and ends at the nearest 25-percent step
    above the observed peak — capped at 100 because the value is a
    percentage. When the dataset is flat zero we still draw a 0-25 axis
    so the empty chart isn't a single horizontal line crammed against
    the bottom.
    """
    observed = max((bucket["pct"] for bucket in buckets), default=0.0)
    if observed <= 0.0:
        return 25.0
    # Round up to the next 25-percent gridline so the y-axis labels stay
    # round numbers (25 / 50 / 75 / 100).
    step = 25.0
    ceiling = step
    while ceiling < observed and ceiling < 100.0:
        ceiling += step
    return min(ceiling, 100.0)


def _build_chart(buckets: list[ErrorRateDay]) -> dict[str, Any]:
    """Project ``buckets`` into SVG coordinates plus polyline metadata.

    Returns a dict consumed verbatim by the template — the geometry
    constants live in this module so the Jinja side stays declarative.
    """
    plot_left = _PAD_LEFT
    plot_right = _SVG_WIDTH - _PAD_RIGHT
    plot_top = _PAD_TOP
    plot_bottom = _SVG_HEIGHT - _PAD_BOTTOM
    plot_width = plot_right - plot_left
    plot_height = plot_bottom - plot_top

    y_max = _y_axis_max(buckets)
    # Guard against division-by-zero — ``_y_axis_max`` never returns 0
    # but mypy can't see that from the signature.
    y_span = y_max if y_max > 0 else 1.0

    point_count = len(buckets)
    x_step = plot_width / (point_count - 1) if point_count > 1 else 0.0

    points: list[dict[str, Any]] = []
    for idx, bucket in enumerate(buckets):
        x = plot_left + idx * x_step if point_count > 1 else plot_left + plot_width / 2
        # Y axis is inverted (SVG origin is top-left); larger pct = closer to the top.
        y = plot_bottom - (bucket["pct"] / y_span) * plot_height
        highlight = bucket["pct"] > _HIGHLIGHT_PCT
        tooltip = (
            f"{bucket['date']} — "
            f"{bucket['low_conf_or_empty']}/{bucket['total_shots']} bad "
            f"({bucket['pct']:.1f}%)"
        )
        points.append(
            {
                "x": x,
                "y": y,
                "pct": bucket["pct"],
                "date": bucket["date"],
                "total_shots": bucket["total_shots"],
                "low_conf_or_empty": bucket["low_conf_or_empty"],
                "highlight": highlight,
                "fill": _DOT_HIGHLIGHT if highlight else _DOT_NORMAL,
                "tooltip": tooltip,
            }
        )

    polyline = " ".join(f"{p['x']:.2f},{p['y']:.2f}" for p in points)

    # Closed polygon under the polyline for the soft fill band. Only
    # emitted when there are at least two points — a single point can't
    # bound a band.
    band = ""
    if len(points) >= 2:
        band_points = [f"{points[0]['x']:.2f},{plot_bottom:.2f}"]
        band_points.extend(f"{p['x']:.2f},{p['y']:.2f}" for p in points)
        band_points.append(f"{points[-1]['x']:.2f},{plot_bottom:.2f}")
        band = " ".join(band_points)

    # Y-axis ticks at every 25% step from 0 up to the chosen ceiling.
    y_ticks: list[dict[str, Any]] = []
    tick_value = 0.0
    while tick_value <= y_max + 0.001:
        ratio = tick_value / y_span
        ty = plot_bottom - ratio * plot_height
        y_ticks.append({"y": ty, "label": f"{int(tick_value)}%"})
        tick_value += 25.0

    # X-axis labels — only show ~6 evenly spaced ticks so the strip
    # stays legible even on the 90-day window.
    x_labels: list[dict[str, Any]] = []
    desired_labels = 6
    if point_count > 0:
        stride = max(1, point_count // desired_labels)
        for idx, bucket in enumerate(buckets):
            if idx % stride != 0 and idx != point_count - 1:
                continue
            x = plot_left + idx * x_step if point_count > 1 else plot_left + plot_width / 2
            # Trim "YYYY-" off the front — the year repeats and clutters
            # the strip in every realistic window size.
            short = bucket["date"][5:]
            x_labels.append({"x": x, "label": short})

    # Horizontal reference line at the highlight threshold so the user
    # can see the cutoff at a glance.
    highlight_y = plot_bottom - (_HIGHLIGHT_PCT / y_span) * plot_height

    return {
        "width": _SVG_WIDTH,
        "height": _SVG_HEIGHT,
        "plot_left": plot_left,
        "plot_right": plot_right,
        "plot_top": plot_top,
        "plot_bottom": plot_bottom,
        "y_max": y_max,
        "points": points,
        "polyline": polyline,
        "band": band,
        "y_ticks": y_ticks,
        "x_labels": x_labels,
        "stroke": _STROKE_NORMAL,
        "fill_band": _FILL_BAND,
        "baseline_colour": _BASELINE_COLOUR,
        "axis_colour": _AXIS_COLOUR,
        "axis_text": _AXIS_TEXT,
        "highlight_y": highlight_y,
        "highlight_pct": _HIGHLIGHT_PCT,
        "highlight_line": _HIGHLIGHT_LINE,
        "dot_normal": _DOT_NORMAL,
        "dot_highlight": _DOT_HIGHLIGHT,
    }


def _summary(buckets: list[ErrorRateDay]) -> dict[str, Any]:
    """Compute headline numbers for the summary tiles."""
    total_shots = sum(b["total_shots"] for b in buckets)
    total_errors = sum(b["low_conf_or_empty"] for b in buckets)
    overall_pct = round((total_errors / total_shots * 100.0), 1) if total_shots > 0 else 0.0
    worst: ErrorRateDay | None = None
    for bucket in buckets:
        if bucket["total_shots"] == 0:
            continue
        if worst is None or bucket["pct"] > worst["pct"]:
            worst = bucket
    highlighted_days = sum(1 for b in buckets if b["pct"] > _HIGHLIGHT_PCT)
    return {
        "total_shots": total_shots,
        "total_errors": total_errors,
        "overall_pct": overall_pct,
        "worst": worst,
        "highlighted_days": highlighted_days,
    }


@router.get("/stats/ocr-error-rate", response_class=HTMLResponse)
async def ocr_error_rate_page(
    request: Request,
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the OCR error-rate dashboard."""
    buckets = await error_rate_by_day(days=days)
    chart = _build_chart(buckets)
    summary = _summary(buckets)

    log.info(
        "ocr.error_rate.page",
        days=days,
        days_in_window=len(buckets),
        total_shots=summary["total_shots"],
        total_errors=summary["total_errors"],
        highlighted_days=summary["highlighted_days"],
    )

    return templates.TemplateResponse(
        request,
        "ocr_error_rate.html",
        {
            "title": "OCR error rate",
            "active_nav": "stats",
            "days": days,
            "min_days": _MIN_DAYS,
            "max_days": _MAX_DAYS,
            "window_choices": _WINDOW_CHOICES,
            "buckets": buckets,
            "chart": chart,
            "summary": summary,
            "highlight_pct": _HIGHLIGHT_PCT,
        },
    )


@router.get("/api/ocr-error-rate.json", response_class=JSONResponse)
async def ocr_error_rate_json(
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> JSONResponse:
    """Return the raw daily error-rate timeseries as JSON.

    The payload echoes the resolved ``days`` window plus a ``threshold``
    field so a client can highlight the same days the HTML view does
    without re-deriving the cutoff. Each entry in ``days_series``
    carries an extra ``highlight`` boolean for the same reason.
    """
    buckets = await error_rate_by_day(days=days)
    days_series: list[dict[str, Any]] = [
        {
            "date": bucket["date"],
            "total_shots": bucket["total_shots"],
            "low_conf_or_empty": bucket["low_conf_or_empty"],
            "pct": bucket["pct"],
            "highlight": bucket["pct"] > _HIGHLIGHT_PCT,
        }
        for bucket in buckets
    ]
    summary = _summary(buckets)
    payload: dict[str, Any] = {
        "days": days,
        "threshold_pct": _HIGHLIGHT_PCT,
        "total_shots": summary["total_shots"],
        "total_errors": summary["total_errors"],
        "overall_pct": summary["overall_pct"],
        "highlighted_days": summary["highlighted_days"],
        "days_series": days_series,
    }
    return JSONResponse(payload)
