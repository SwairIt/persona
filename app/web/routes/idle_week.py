"""Weekly idle-vs-active stacked bar chart — HTML page and JSON endpoint.

The page renders a pure-SVG 480x240 stacked bar chart: one bar per day for
the 7-day window ending at ``end_date`` (default = today). Each bar stacks
emerald (active) on the bottom and amber (idle) on top, with day labels
below the X-axis baseline. Logic lives in :mod:`app.idle_week`; this
module is a thin presentation shell.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.idle_stats import DEFAULT_IDLE_THRESHOLD_S, DEFAULT_MAX_GAP_S
from app.idle_week import WINDOW_DAYS, WeeklyIdleDay, weekly_idle
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.idle_week")

router = APIRouter(tags=["idle-week"])

# SVG geometry — pure dimensions, no JS / no animation deps.
_SVG_WIDTH = 480
_SVG_HEIGHT = 240
_PAD_LEFT = 36
_PAD_RIGHT = 12
_PAD_TOP = 12
_PAD_BOTTOM = 36  # room for day labels along the X axis
_BAR_GAP = 8

_FILL_ACTIVE = "#10b981"  # emerald-500
_FILL_IDLE = "#f59e0b"  # amber-500
_FILL_EMPTY = "#3f3f46"  # zinc-700 — drawn when both buckets are zero

_WEEKDAY_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _parse_end_date(value: str | None) -> date:
    """Parse ``YYYY-MM-DD``; fall back to today on bad / missing input."""
    if value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            log.warning("idle_week.bad_end_date_param", end_date=value)
    return datetime.now().astimezone().date()


def _format_hm(seconds: int) -> str:
    """Format ``seconds`` as ``H:MM`` (no seconds, plenty wide for chart UI)."""
    safe = max(int(seconds), 0)
    hours, remainder = divmod(safe, 3600)
    minutes = remainder // 60
    return f"{hours}:{minutes:02d}"


def _format_hms(seconds: int) -> str:
    """Format ``seconds`` as ``H:MM:SS`` — for JSON parity with /api/idle."""
    safe = max(int(seconds), 0)
    minutes, sec = divmod(safe, 60)
    hours, mins = divmod(minutes, 60)
    return f"{hours}:{mins:02d}:{sec:02d}"


def _weekday_label(day_iso: str) -> str:
    """Return ``Mon`` / ``Tue`` / ... for a ``YYYY-MM-DD`` ISO date."""
    try:
        return _WEEKDAY_LABELS[date.fromisoformat(day_iso).weekday()]
    except (ValueError, IndexError):
        return "?"


def _build_bars(days: list[WeeklyIdleDay]) -> list[dict[str, object]]:
    """Map the 7-day window onto SVG-ready stacked-bar descriptors.

    Heights are scaled to the *max day total* (active + idle) so the
    busiest day fills the plot vertically. Days with zero attended seconds
    render a faint 1px empty stub on the baseline so the X-axis still
    reads cleanly.
    """
    plot_width = _SVG_WIDTH - _PAD_LEFT - _PAD_RIGHT
    plot_height = _SVG_HEIGHT - _PAD_TOP - _PAD_BOTTOM
    if len(days) == 0:
        return []
    bar_width = (plot_width - _BAR_GAP * (len(days) - 1)) / len(days)
    baseline_y = _PAD_TOP + plot_height

    max_total = max(
        (d["active_seconds"] + d["idle_seconds"] for d in days), default=0
    )

    bars: list[dict[str, object]] = []
    for index, day in enumerate(days):
        x = _PAD_LEFT + index * (bar_width + _BAR_GAP)
        active_s = day["active_seconds"]
        idle_s = day["idle_seconds"]
        total_s = active_s + idle_s

        if max_total > 0 and total_s > 0:
            total_h = total_s / max_total * plot_height
            active_h = active_s / total_s * total_h if total_s else 0.0
            idle_h = total_h - active_h
        else:
            total_h = 0.0
            active_h = 0.0
            idle_h = 0.0

        # Active stacks at the bottom, idle on top.
        active_y = baseline_y - active_h
        idle_y = active_y - idle_h
        # Empty-day stub (1px on baseline) so the bin is still locatable.
        empty_visible = total_s == 0

        weekday = _weekday_label(day["date"])
        bars.append(
            {
                "date": day["date"],
                "weekday": weekday,
                "active_seconds": active_s,
                "idle_seconds": idle_s,
                "total_seconds": total_s,
                "x": x,
                "width": bar_width,
                "active_y": active_y,
                "active_height": active_h,
                "idle_y": idle_y,
                "idle_height": idle_h,
                "empty_visible": empty_visible,
                "empty_y": baseline_y - 1.0,
                "empty_height": 1.0,
                "label_x": x + bar_width / 2,
                "label_weekday": weekday,
                "label_date": day["date"][5:],  # MM-DD — keeps X-axis tidy.
                "tooltip": (
                    f"{weekday} {day['date']} — "
                    f"active {_format_hm(active_s)}, "
                    f"idle {_format_hm(idle_s)}"
                ),
            }
        )
    return bars


def _y_axis_ticks(max_total_seconds: int) -> list[dict[str, object]]:
    """Render 3 evenly spaced Y-axis labels (0, mid, max) in ``H:MM``.

    For an empty window we still emit a single ``0:00`` tick so the axis
    isn't bare.
    """
    plot_height = _SVG_HEIGHT - _PAD_TOP - _PAD_BOTTOM
    baseline_y = _PAD_TOP + plot_height

    if max_total_seconds <= 0:
        return [{"y": baseline_y, "label": "0:00"}]

    ticks: list[dict[str, object]] = []
    for fraction, value in (
        (0.0, 0),
        (0.5, max_total_seconds // 2),
        (1.0, max_total_seconds),
    ):
        ticks.append(
            {
                "y": baseline_y - fraction * plot_height,
                "label": _format_hm(value),
            }
        )
    return ticks


@router.get("/stats/idle-week", response_class=HTMLResponse)
async def idle_week_page(
    request: Request,
    end_date: str | None = Query(default=None),
    threshold: int = Query(
        default=DEFAULT_IDLE_THRESHOLD_S, ge=1, le=86_400,
    ),
) -> HTMLResponse:
    target_end = _parse_end_date(end_date)
    target_start = target_end - timedelta(days=WINDOW_DAYS - 1)
    days = await weekly_idle(
        target_end.isoformat(),
        idle_threshold_s=threshold,
        max_gap_s=DEFAULT_MAX_GAP_S,
    )

    total_active = sum(d["active_seconds"] for d in days)
    total_idle = sum(d["idle_seconds"] for d in days)
    total_attended = total_active + total_idle

    active_pct = (
        (total_active / total_attended * 100.0) if total_attended else 0.0
    )
    idle_pct = (
        (total_idle / total_attended * 100.0) if total_attended else 0.0
    )

    bars = _build_bars(days)
    max_total = max(
        (d["active_seconds"] + d["idle_seconds"] for d in days), default=0
    )

    prev_end = (target_end - timedelta(days=WINDOW_DAYS)).isoformat()
    next_end = (target_end + timedelta(days=WINDOW_DAYS)).isoformat()
    today_iso = datetime.now().astimezone().date().isoformat()

    return templates.TemplateResponse(
        request,
        "idle_week.html",
        {
            "title": f"Idle week · {target_end.isoformat()}",
            "active_nav": "stats",
            "end_date_iso": target_end.isoformat(),
            "start_date_iso": target_start.isoformat(),
            "prev_end": prev_end,
            "next_end": next_end,
            "today_iso": today_iso,
            "threshold": threshold,
            "days": days,
            "bars": bars,
            "total_active_seconds": total_active,
            "total_idle_seconds": total_idle,
            "total_attended_seconds": total_attended,
            "total_active_duration": _format_hms(total_active),
            "total_idle_duration": _format_hms(total_idle),
            "total_attended_duration": _format_hms(total_attended),
            "active_percent": active_pct,
            "idle_percent": idle_pct,
            "max_total_seconds": max_total,
            "svg_width": _SVG_WIDTH,
            "svg_height": _SVG_HEIGHT,
            "pad_left": _PAD_LEFT,
            "pad_right": _PAD_RIGHT,
            "pad_top": _PAD_TOP,
            "pad_bottom": _PAD_BOTTOM,
            "baseline_y": _SVG_HEIGHT - _PAD_BOTTOM,
            "y_ticks": _y_axis_ticks(max_total),
            "fill_active": _FILL_ACTIVE,
            "fill_idle": _FILL_IDLE,
            "fill_empty": _FILL_EMPTY,
        },
    )


@router.get("/api/idle-week.json", response_class=JSONResponse)
async def idle_week_json(
    end_date: str | None = Query(default=None),
    threshold: int = Query(
        default=DEFAULT_IDLE_THRESHOLD_S, ge=1, le=86_400,
    ),
) -> JSONResponse:
    target_end = _parse_end_date(end_date)
    target_start = target_end - timedelta(days=WINDOW_DAYS - 1)
    days = await weekly_idle(
        target_end.isoformat(),
        idle_threshold_s=threshold,
        max_gap_s=DEFAULT_MAX_GAP_S,
    )
    total_active = sum(d["active_seconds"] for d in days)
    total_idle = sum(d["idle_seconds"] for d in days)

    return JSONResponse(
        {
            "start_date": target_start.isoformat(),
            "end_date": target_end.isoformat(),
            "window_days": WINDOW_DAYS,
            "threshold_seconds": threshold,
            "total_active_seconds": total_active,
            "total_idle_seconds": total_idle,
            "total_active_duration": _format_hms(total_active),
            "total_idle_duration": _format_hms(total_idle),
            "days": [dict(d) for d in days],
        }
    )
