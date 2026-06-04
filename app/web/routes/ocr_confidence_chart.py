"""Per-app OCR confidence chart — `/stats/ocr-confidence` page + JSON API.

Renders :func:`app.ocr_confidence_stats.compute_app_confidence` as a
horizontal SVG bar chart, one row per app, sorted worst-mean-conf at
the top. Each bar is colour-banded against the same red / amber / green
cutoffs the OCR overlay uses (red ``< 50``, amber ``50-75``, green
``> 75``) so the operator can pattern-match against the rest of the
OCR stats UI at a glance.

Layout (paddings, row geometry, label projections) is computed in this
module rather than in the template so the Jinja side stays declarative.
"""

from __future__ import annotations

from typing import Any, Final

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.ocr_confidence_stats import AppConfidence, compute_app_confidence
from app.web.templates_engine import templates

router = APIRouter(tags=["stats"])
log = get_logger("persona.ocr_confidence")

# Mirrors :data:`app.ocr_confidence_stats._MAX_DAYS` so FastAPI's query
# validator returns a 422 before the data layer even starts the scan.
_MIN_DAYS: Final[int] = 1
_MAX_DAYS: Final[int] = 365
_DEFAULT_DAYS: Final[int] = 7

# Quick-select buttons for the window switcher.
_WINDOW_CHOICES: Final[tuple[int, ...]] = (1, 3, 7, 14, 30, 60)

# Colour bands — match the OCR overlay palette so the operator's mental
# model carries straight across pages.
_LOW_CUTOFF: Final[float] = 50.0
_HIGH_CUTOFF: Final[float] = 75.0

_COLOUR_LOW: Final[str] = "#ef4444"  # red-500
_COLOUR_MID: Final[str] = "#f59e0b"  # amber-500
_COLOUR_HIGH: Final[str] = "#22c55e"  # green-500
_BAR_TRACK: Final[str] = "#27272a"  # zinc-800
_AXIS_TEXT: Final[str] = "#71717a"  # zinc-500
_LABEL_TEXT: Final[str] = "#e4e4e7"  # zinc-200
_GRID_COLOUR: Final[str] = "#3f3f46"  # zinc-700

# SVG layout — width is fixed so the chart sits inside the standard
# stats card; height grows with the number of rows. Per-row geometry
# keeps ~28 px so 12-20 apps fit comfortably on one screen without
# scrolling.
_SVG_WIDTH: Final[int] = 720
_ROW_HEIGHT: Final[int] = 28
_PAD_TOP: Final[int] = 18
_PAD_BOTTOM: Final[int] = 26
_PAD_LEFT: Final[int] = 170  # room for app-name labels
_PAD_RIGHT: Final[int] = 90  # room for the right-side word-count badge
_EMPTY_HEIGHT: Final[int] = 120  # placeholder height when no data


def _bar_colour(avg: float) -> str:
    """Map a mean conf value to its bar colour.

    Red ``< 50``, amber ``50-75``, green ``> 75``. The cutoffs mirror
    the OCR overlay palette so the operator's mental model carries
    cleanly across pages.
    """
    if avg < _LOW_CUTOFF:
        return _COLOUR_LOW
    if avg <= _HIGH_CUTOFF:
        return _COLOUR_MID
    return _COLOUR_HIGH


def _truncate_label(label: str, max_chars: int = 22) -> str:
    """Truncate ``label`` with an ellipsis so the y-axis stays legible.

    The 22-char ceiling keeps even verbose Windows window titles (e.g.
    ``WindowsTerminal.exe``) fitting inside the ``_PAD_LEFT`` strip
    without overlapping the bar track.
    """
    if len(label) <= max_chars:
        return label
    return label[: max_chars - 1] + "…"


def _build_chart(rows: list[AppConfidence]) -> dict[str, Any]:
    """Project ``rows`` into SVG coordinates plus per-bar metadata.

    Returns a dict consumed verbatim by the template — geometry
    constants live in this module so the Jinja side stays declarative.
    """
    bar_left = _PAD_LEFT
    bar_right = _SVG_WIDTH - _PAD_RIGHT
    bar_track_width = bar_right - bar_left

    bars: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        # Each bar's centreline; the rect sits above by half its height
        # so the label baseline aligns with the bar's vertical centre.
        cy = _PAD_TOP + idx * _ROW_HEIGHT + _ROW_HEIGHT / 2
        # 100 is the max possible conf — scale the bar relative to that
        # fixed ceiling rather than the observed max so cross-window
        # comparisons stay honest (a window where every app is bad
        # shouldn't make the worst app look good by re-scaling).
        bar_width = (row["avg"] / 100.0) * bar_track_width
        tooltip = (
            f"{row['app_name']} — avg {row['avg']:.1f}, "
            f"p10 {row['p10']:.1f}, p90 {row['p90']:.1f} "
            f"({row['words']:,} words)"
        )
        bars.append(
            {
                "y": cy - _ROW_HEIGHT / 2 + 4,
                "label_y": cy + 4,  # baseline tweak for SVG text
                "label": _truncate_label(row["app_name"]),
                "label_full": row["app_name"],
                "bar_y": cy - 6,
                "bar_height": 12,
                "bar_width": bar_width,
                "bar_x": bar_left,
                "fill": _bar_colour(row["avg"]),
                "avg": row["avg"],
                "p10": row["p10"],
                "p90": row["p90"],
                "words": row["words"],
                "value_x": bar_left + bar_width + 6,
                "value_y": cy + 4,
                "badge_x": bar_right + 8,
                "badge_y": cy + 4,
                "tooltip": tooltip,
            }
        )

    # Reference gridlines at the colour-band cutoffs so the operator can
    # eyeball "this app's mean is on the wrong side of the threshold"
    # without reading the numeric label.
    gridlines: list[dict[str, Any]] = []
    chart_top = _PAD_TOP - 6
    chart_bottom = (
        _PAD_TOP + len(rows) * _ROW_HEIGHT
        if rows
        else _PAD_TOP + _EMPTY_HEIGHT
    )
    for cutoff, label in ((0.0, "0"), (_LOW_CUTOFF, "50"), (_HIGH_CUTOFF, "75"), (100.0, "100")):
        x = bar_left + (cutoff / 100.0) * bar_track_width
        gridlines.append(
            {
                "x": x,
                "y1": chart_top,
                "y2": chart_bottom,
                "label": label,
                "label_y": chart_bottom + 14,
            }
        )

    total_height = (
        _PAD_TOP + len(rows) * _ROW_HEIGHT + _PAD_BOTTOM
        if rows
        else _PAD_TOP + _EMPTY_HEIGHT + _PAD_BOTTOM
    )

    return {
        "width": _SVG_WIDTH,
        "height": total_height,
        "bar_left": bar_left,
        "bar_right": bar_right,
        "bars": bars,
        "gridlines": gridlines,
        "axis_text": _AXIS_TEXT,
        "label_text": _LABEL_TEXT,
        "grid_colour": _GRID_COLOUR,
        "bar_track": _BAR_TRACK,
        "low_cutoff": _LOW_CUTOFF,
        "high_cutoff": _HIGH_CUTOFF,
        "colour_low": _COLOUR_LOW,
        "colour_mid": _COLOUR_MID,
        "colour_high": _COLOUR_HIGH,
    }


def _summary(rows: list[AppConfidence]) -> dict[str, Any]:
    """Compute headline numbers for the summary tiles."""
    total_words = sum(r["words"] for r in rows)
    total_apps = len(rows)
    worst: AppConfidence | None = rows[0] if rows else None
    best: AppConfidence | None = rows[-1] if rows else None
    if total_words > 0:
        overall_avg = round(
            sum(r["avg"] * r["words"] for r in rows) / total_words,
            1,
        )
    else:
        overall_avg = 0.0
    return {
        "total_apps": total_apps,
        "total_words": total_words,
        "overall_avg": overall_avg,
        "worst": worst,
        "best": best,
    }


@router.get("/stats/ocr-confidence", response_class=HTMLResponse)
async def ocr_confidence_page(
    request: Request,
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the per-app OCR confidence dashboard."""
    rows = await compute_app_confidence(days=days)
    chart = _build_chart(rows)
    summary = _summary(rows)

    log.info(
        "ocr_confidence.page",
        days=days,
        apps=summary["total_apps"],
        total_words=summary["total_words"],
        overall_avg=summary["overall_avg"],
    )

    return templates.TemplateResponse(
        request,
        "ocr_confidence.html",
        {
            "title": "Качество OCR по приложениям",
            "active_nav": "stats",
            "days": days,
            "min_days": _MIN_DAYS,
            "max_days": _MAX_DAYS,
            "window_choices": _WINDOW_CHOICES,
            "rows": rows,
            "chart": chart,
            "summary": summary,
        },
    )


@router.get("/api/stats/ocr-confidence.json", response_class=JSONResponse)
async def ocr_confidence_json(
    days: int = Query(default=_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> JSONResponse:
    """Return the raw per-app confidence rows as JSON.

    Payload echoes the resolved ``days`` window plus headline aggregates
    so a client can render its own summary without walking ``apps``
    twice.
    """
    rows = await compute_app_confidence(days=days)
    summary = _summary(rows)
    apps_payload: list[dict[str, Any]] = [
        {
            "app_name": r["app_name"],
            "words": r["words"],
            "avg": r["avg"],
            "p10": r["p10"],
            "p90": r["p90"],
        }
        for r in rows
    ]
    worst = summary["worst"]
    best = summary["best"]
    payload: dict[str, Any] = {
        "days": days,
        "total_apps": summary["total_apps"],
        "total_words": summary["total_words"],
        "overall_avg": summary["overall_avg"],
        "worst_app": dict(worst) if worst is not None else None,
        "best_app": dict(best) if best is not None else None,
        "apps": apps_payload,
    }
    return JSONResponse(payload)
