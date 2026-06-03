"""Per-day OCR character-count chart — `/stats/ocr-length` page + JSON API.

Renders the daily ``total_chars`` timeseries produced by
:mod:`app.ocr_length_chart` as a 480x180 SVG area chart. The primary
y-axis is anchored on ``total_chars``; a secondary axis hint (right-edge
labels and a thin dashed reference) is overlaid for the
``avg_chars_per_shot`` series so the operator can correlate "more text
captured" against "denser shots" at a glance.

The chart layout (paddings, axis ticks, polyline points, area band) is
computed in this module rather than the template so the HTML stays
focused on markup and the projected geometry is straightforward to
test in isolation.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.ocr_length_chart import OcrLengthDay, daily_length
from app.web.templates_engine import templates

router = APIRouter(tags=["stats"])
log = get_logger("persona.ocr.length")

# Mirrors :data:`app.ocr_length_chart._MAX_DAYS` — duplicated here so
# FastAPI's query-parameter validator returns a 422 before the data
# layer even starts the scan.
_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 365
_DEFAULT_DAYS: Final[int] = 60

# Quick-select buttons for the window switcher. 60 is the default and
# matches the data-layer default.
_WINDOW_CHOICES: Final[tuple[int, ...]] = (7, 14, 30, 60, 90, 180)

# SVG geometry. The chart is fixed at 480x180 per the spec; paddings
# reserve room for left-axis (total chars) and right-axis (avg/shot)
# labels without crowding the polyline.
_SVG_WIDTH: Final[int] = 480
_SVG_HEIGHT: Final[int] = 180
_PAD_LEFT: Final[int] = 42
_PAD_RIGHT: Final[int] = 42
_PAD_TOP: Final[int] = 14
_PAD_BOTTOM: Final[int] = 26

# Colours — match the existing dark-mode palette used by the other
# stats pages. Primary series is the accent violet; the secondary
# avg/shot hint is a muted amber so the two read distinctly without
# fighting for attention.
_STROKE_PRIMARY: Final[str] = "#a78bfa"  # accent-400
_FILL_BAND: Final[str] = "rgba(167, 139, 250, 0.18)"
_DOT_PRIMARY: Final[str] = "#a78bfa"
_STROKE_AVG: Final[str] = "#fbbf24"  # amber-400
_DOT_AVG: Final[str] = "#fbbf24"
_BASELINE_COLOUR: Final[str] = "#3f3f46"
_AXIS_COLOUR: Final[str] = "#52525b"
_AXIS_TEXT: Final[str] = "#71717a"
_AXIS_TEXT_AVG: Final[str] = "#b45309"  # amber-700 — readable on dark


def _round_up_nice(value: float) -> float:
    """Round ``value`` up to a "nice" axis ceiling.

    Picks the smallest of ``{1, 2, 2.5, 5} * 10^n`` that's >= ``value``
    so y-axis labels land on round numbers (100, 250, 500, ...). For
    inputs <= 0 returns ``1.0`` so the axis never collapses.
    """
    if value <= 0:
        return 1.0
    # Find the order-of-magnitude bucket.
    magnitude = 1.0
    while magnitude * 10 <= value:
        magnitude *= 10.0
    while magnitude > value:
        magnitude /= 10.0
    for step in (1.0, 2.0, 2.5, 5.0, 10.0):
        candidate = step * magnitude
        if candidate >= value:
            return candidate
    return magnitude * 10.0


def _format_axis(value: float) -> str:
    """Compact left/right-axis label — ``35``, ``1.2k``, ``35k``, ``1.4M``.

    Picks a unit suffix that keeps the label at most four glyphs wide
    so the y-axis strip doesn't crowd the polyline at the default
    480px chart width.
    """
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        rounded = value / 1_000
        if rounded >= 100:
            return f"{round(rounded)}k"
        return f"{rounded:.1f}k".replace(".0k", "k")
    return f"{round(value)}"


def _build_chart(buckets: list[OcrLengthDay]) -> dict[str, Any]:
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

    # Primary axis — total_chars. Use a "nice" ceiling so labels are
    # round numbers. When the window is empty we still draw a 0-1000
    # axis so the chart isn't a single horizontal line.
    observed_total = max((b["total_chars"] for b in buckets), default=0)
    y_max_total = _round_up_nice(float(observed_total)) if observed_total > 0 else 1000.0
    total_span = y_max_total if y_max_total > 0 else 1.0

    # Secondary axis — avg_chars_per_shot. Its own ceiling so the two
    # series can coexist visually without one crushing the other.
    observed_avg = max((b["avg_chars_per_shot"] for b in buckets), default=0.0)
    y_max_avg = _round_up_nice(observed_avg) if observed_avg > 0 else 100.0
    avg_span = y_max_avg if y_max_avg > 0 else 1.0

    point_count = len(buckets)
    x_step = plot_width / (point_count - 1) if point_count > 1 else 0.0

    primary_points: list[dict[str, Any]] = []
    avg_points: list[dict[str, Any]] = []
    for idx, bucket in enumerate(buckets):
        x = (
            plot_left + idx * x_step
            if point_count > 1
            else plot_left + plot_width / 2
        )
        # Y axis is inverted (SVG origin is top-left).
        y_primary = (
            plot_bottom - (bucket["total_chars"] / total_span) * plot_height
        )
        y_avg = (
            plot_bottom - (bucket["avg_chars_per_shot"] / avg_span) * plot_height
        )
        tooltip = (
            f"{bucket['date']} — "
            f"{bucket['total_chars']:,} chars across "
            f"{bucket['shot_count']} shot"
            f"{'' if bucket['shot_count'] == 1 else 's'} "
            f"(avg {bucket['avg_chars_per_shot']:.1f}/shot)"
        )
        primary_points.append(
            {
                "x": x,
                "y": y_primary,
                "date": bucket["date"],
                "total_chars": bucket["total_chars"],
                "shot_count": bucket["shot_count"],
                "avg_chars_per_shot": bucket["avg_chars_per_shot"],
                "fill": _DOT_PRIMARY,
                "tooltip": tooltip,
            }
        )
        avg_points.append(
            {
                "x": x,
                "y": y_avg,
                "avg_chars_per_shot": bucket["avg_chars_per_shot"],
                "fill": _DOT_AVG,
            }
        )

    polyline_primary = " ".join(
        f"{p['x']:.2f},{p['y']:.2f}" for p in primary_points
    )
    polyline_avg = " ".join(
        f"{p['x']:.2f},{p['y']:.2f}" for p in avg_points
    )

    # Closed polygon under the primary polyline — the area band that
    # makes the chart read as "how much text" rather than just a line.
    # Only emitted when there are at least two points; a single point
    # can't bound a band.
    band = ""
    if len(primary_points) >= 2:
        band_points = [f"{primary_points[0]['x']:.2f},{plot_bottom:.2f}"]
        band_points.extend(
            f"{p['x']:.2f},{p['y']:.2f}" for p in primary_points
        )
        band_points.append(f"{primary_points[-1]['x']:.2f},{plot_bottom:.2f}")
        band = " ".join(band_points)

    # Y-axis ticks — four evenly spaced stops (0, 1/3, 2/3, max).
    y_ticks_total: list[dict[str, Any]] = []
    y_ticks_avg: list[dict[str, Any]] = []
    for fraction in (0.0, 1.0 / 3.0, 2.0 / 3.0, 1.0):
        ty = plot_bottom - fraction * plot_height
        y_ticks_total.append(
            {
                "y": ty,
                "label": _format_axis(fraction * y_max_total),
            }
        )
        y_ticks_avg.append(
            {
                "y": ty,
                "label": _format_axis(fraction * y_max_avg),
            }
        )

    # X-axis labels — show ~6 evenly spaced ticks so the strip stays
    # legible even on the 180-day window.
    x_labels: list[dict[str, Any]] = []
    desired_labels = 6
    if point_count > 0:
        stride = max(1, point_count // desired_labels)
        for idx, bucket in enumerate(buckets):
            if idx % stride != 0 and idx != point_count - 1:
                continue
            x = (
                plot_left + idx * x_step
                if point_count > 1
                else plot_left + plot_width / 2
            )
            # Trim "YYYY-" off the front — the year repeats and clutters
            # the strip in every realistic window size.
            short = bucket["date"][5:]
            x_labels.append({"x": x, "label": short})

    return {
        "width": _SVG_WIDTH,
        "height": _SVG_HEIGHT,
        "plot_left": plot_left,
        "plot_right": plot_right,
        "plot_top": plot_top,
        "plot_bottom": plot_bottom,
        "y_max_total": y_max_total,
        "y_max_avg": y_max_avg,
        "primary_points": primary_points,
        "avg_points": avg_points,
        "polyline_primary": polyline_primary,
        "polyline_avg": polyline_avg,
        "band": band,
        "y_ticks_total": y_ticks_total,
        "y_ticks_avg": y_ticks_avg,
        "x_labels": x_labels,
        "stroke_primary": _STROKE_PRIMARY,
        "fill_band": _FILL_BAND,
        "stroke_avg": _STROKE_AVG,
        "dot_primary": _DOT_PRIMARY,
        "dot_avg": _DOT_AVG,
        "baseline_colour": _BASELINE_COLOUR,
        "axis_colour": _AXIS_COLOUR,
        "axis_text": _AXIS_TEXT,
        "axis_text_avg": _AXIS_TEXT_AVG,
    }


def _summary(buckets: list[OcrLengthDay]) -> dict[str, Any]:
    """Compute headline numbers for the summary tiles."""
    total_chars = sum(b["total_chars"] for b in buckets)
    total_shots = sum(b["shot_count"] for b in buckets)
    overall_avg = (
        round(total_chars / total_shots, 1) if total_shots > 0 else 0.0
    )

    peak: OcrLengthDay | None = None
    for bucket in buckets:
        if bucket["total_chars"] <= 0:
            continue
        if peak is None or bucket["total_chars"] > peak["total_chars"]:
            peak = bucket

    densest: OcrLengthDay | None = None
    for bucket in buckets:
        if bucket["shot_count"] <= 0:
            continue
        if (
            densest is None
            or bucket["avg_chars_per_shot"] > densest["avg_chars_per_shot"]
        ):
            densest = bucket

    non_empty_days = sum(1 for b in buckets if b["shot_count"] > 0)

    return {
        "total_chars": total_chars,
        "total_shots": total_shots,
        "overall_avg": overall_avg,
        "peak": peak,
        "densest": densest,
        "non_empty_days": non_empty_days,
    }


@router.get("/stats/ocr-length", response_class=HTMLResponse)
async def ocr_length_page(
    request: Request,
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the per-day OCR character-count dashboard."""
    buckets = await daily_length(days=days)
    chart = _build_chart(buckets)
    summary = _summary(buckets)

    log.info(
        "ocr.length.page",
        days=days,
        days_in_window=len(buckets),
        total_chars=summary["total_chars"],
        total_shots=summary["total_shots"],
        non_empty_days=summary["non_empty_days"],
    )

    return templates.TemplateResponse(
        request,
        "ocr_length_chart.html",
        {
            "title": "OCR character count per day",
            "active_nav": "stats",
            "days": days,
            "min_days": _MIN_DAYS,
            "max_days": _MAX_DAYS,
            "window_choices": _WINDOW_CHOICES,
            "buckets": buckets,
            "chart": chart,
            "summary": summary,
        },
    )


@router.get("/api/ocr-length.json", response_class=JSONResponse)
async def ocr_length_json(
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> JSONResponse:
    """Return the raw daily character-count timeseries as JSON.

    The payload echoes the resolved ``days`` window plus headline
    aggregates so a client can render its own summary tiles without
    walking the ``days_series`` array twice.
    """
    buckets = await daily_length(days=days)
    summary = _summary(buckets)
    days_series: list[dict[str, Any]] = [
        {
            "date": bucket["date"],
            "total_chars": bucket["total_chars"],
            "avg_chars_per_shot": bucket["avg_chars_per_shot"],
            "shot_count": bucket["shot_count"],
        }
        for bucket in buckets
    ]
    peak = summary["peak"]
    densest = summary["densest"]
    payload: dict[str, Any] = {
        "days": days,
        "total_chars": summary["total_chars"],
        "total_shots": summary["total_shots"],
        "overall_avg_chars_per_shot": summary["overall_avg"],
        "non_empty_days": summary["non_empty_days"],
        "peak_day": dict(peak) if peak is not None else None,
        "densest_day": dict(densest) if densest is not None else None,
        "days_series": days_series,
    }
    return JSONResponse(payload)
