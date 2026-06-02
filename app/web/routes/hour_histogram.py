"""Hour-of-day capture-activity histogram — HTML page and JSON endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.hour_histogram import HourBucket, hourly_distribution
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.hours")

router = APIRouter(tags=["hours"])

# Pre-defined day-windows surfaced as quick-pick buttons in the UI.
_WINDOW_CHOICES: tuple[int, ...] = (7, 30, 90, 365)

# SVG geometry — pure dimensions, no JS / no animation deps.
_SVG_WIDTH = 480
_SVG_HEIGHT = 180
_PAD_LEFT = 28
_PAD_RIGHT = 8
_PAD_TOP = 8
_PAD_BOTTOM = 22  # room for hour labels along the X axis
_BAR_GAP = 2

_FILL_ACTIVE = "#10b981"  # emerald-500
_FILL_EMPTY = "#404040"  # neutral-700


def _peak(buckets: list[HourBucket]) -> HourBucket | None:
    """Return the bucket with the highest count, or ``None`` if all empty."""
    best: HourBucket | None = None
    for bucket in buckets:
        if bucket["count"] <= 0:
            continue
        if best is None or bucket["count"] > best["count"]:
            best = bucket
    return best


def _build_bars(buckets: list[HourBucket]) -> list[dict[str, object]]:
    """Map dense 24-entry buckets to SVG-ready rectangle descriptors.

    Heights are scaled to the *max* bucket so the tallest bar always reaches
    the plot's top — an empty window collapses to zero-height bars rendered
    in the empty fill so the X-axis still reads cleanly.
    """
    plot_width = _SVG_WIDTH - _PAD_LEFT - _PAD_RIGHT
    plot_height = _SVG_HEIGHT - _PAD_TOP - _PAD_BOTTOM
    bar_width = (plot_width - _BAR_GAP * (len(buckets) - 1)) / len(buckets)

    max_count = max((b["count"] for b in buckets), default=0)
    baseline_y = _PAD_TOP + plot_height

    bars: list[dict[str, object]] = []
    for index, bucket in enumerate(buckets):
        x = _PAD_LEFT + index * (bar_width + _BAR_GAP)
        if max_count > 0 and bucket["count"] > 0:
            height = bucket["count"] / max_count * plot_height
        else:
            # Empty bar — show a faint 1px stub so the bin is still locatable.
            height = 1.0 if bucket["count"] == 0 else 0.0
        y = baseline_y - height
        fill = _FILL_ACTIVE if bucket["count"] > 0 else _FILL_EMPTY
        label = f"{bucket['hour']:02d}"
        bars.append(
            {
                "hour": bucket["hour"],
                "count": bucket["count"],
                "pct": bucket["pct"],
                "label": label,
                "label_visible": bucket["hour"] % 3 == 0,
                "x": x,
                "y": y,
                "width": bar_width,
                "height": height,
                "label_x": x + bar_width / 2,
                "fill": fill,
                "tooltip": (
                    f"{label}:00 — {bucket['count']} "
                    f"shot{'' if bucket['count'] == 1 else 's'} "
                    f"({bucket['pct']:.1f}%)"
                ),
            }
        )
    return bars


def _y_axis_ticks(max_count: int) -> list[dict[str, object]]:
    """Render 3 evenly spaced Y-axis labels (0, mid, max).

    For an empty window we still emit a single ``0`` tick so the axis isn't
    bare.
    """
    plot_height = _SVG_HEIGHT - _PAD_TOP - _PAD_BOTTOM
    baseline_y = _PAD_TOP + plot_height

    if max_count <= 0:
        return [{"y": baseline_y, "label": "0"}]

    ticks: list[dict[str, object]] = []
    for fraction, value in ((0.0, 0), (0.5, max_count // 2), (1.0, max_count)):
        ticks.append(
            {
                "y": baseline_y - fraction * plot_height,
                "label": str(value),
            }
        )
    return ticks


def _clamp_window(days: int) -> int:
    """Clamp ``days`` to a sensible UI range — mirrors the model-side guard."""
    if days < 1:
        return 1
    if days > 3650:
        return 3650
    return days


@router.get("/hours", response_class=HTMLResponse)
async def hours_page(
    request: Request,
    days: int = Query(default=30, ge=1, le=3650),
) -> HTMLResponse:
    window = _clamp_window(days)
    buckets = await hourly_distribution(days=window)
    bars = _build_bars(buckets)
    total = sum(b["count"] for b in buckets)
    peak = _peak(buckets)
    max_count = max((b["count"] for b in buckets), default=0)

    return templates.TemplateResponse(
        request,
        "hours.html",
        {
            "title": f"Hour histogram · last {window} days",
            "active_nav": "stats",
            "days": window,
            "window_choices": _WINDOW_CHOICES,
            "buckets": buckets,
            "bars": bars,
            "total": total,
            "peak": peak,
            "max_count": max_count,
            "svg_width": _SVG_WIDTH,
            "svg_height": _SVG_HEIGHT,
            "pad_left": _PAD_LEFT,
            "pad_right": _PAD_RIGHT,
            "pad_top": _PAD_TOP,
            "pad_bottom": _PAD_BOTTOM,
            "baseline_y": _SVG_HEIGHT - _PAD_BOTTOM,
            "y_ticks": _y_axis_ticks(max_count),
            "fill_active": _FILL_ACTIVE,
            "fill_empty": _FILL_EMPTY,
        },
    )


@router.get("/api/hours.json", response_class=JSONResponse)
async def hours_json(
    days: int = Query(default=30, ge=1, le=3650),
) -> JSONResponse:
    window = _clamp_window(days)
    buckets = await hourly_distribution(days=window)
    total = sum(b["count"] for b in buckets)
    peak = _peak(buckets)
    return JSONResponse(
        {
            "days": window,
            "total": total,
            "peak_hour": peak["hour"] if peak is not None else None,
            "peak_count": peak["count"] if peak is not None else 0,
            "buckets": [dict(b) for b in buckets],
        }
    )
