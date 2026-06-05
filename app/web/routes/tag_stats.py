"""Per-tag stats card — HTML page + JSON endpoint.

Both surfaces reuse :func:`app.tag_stats.compute_tag_stats` so the
numbers on the page are byte-identical to those in
``/api/tag/{tag}/stats.json``. The HTML page renders the 30-day timeline
as a pure inline ``<polyline>`` SVG sparkline — no JS, no canvas, no
external chart dependency — matching the visual idiom established by
:mod:`app.web.routes.tag_trends`.

The route is registered with the FastAPI router exported by
``router`` below. Wiring the include is the job of the application
factory; this module deliberately does not touch ``app.web.main``.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.tag_stats import DailyEntry, TagStats, compute_tag_stats
from app.web.templates_engine import templates

log = get_logger("persona.tag_stats")

router = APIRouter(tags=["tag-stats"])

# Sparkline geometry — 320x60 inline SVG polyline, matches tag_trends.
_SVG_WIDTH = 320
_SVG_HEIGHT = 60
_PAD_X = 4
_PAD_Y = 6
_STROKE = "#a78bfa"  # accent-400
_FILL_BAND = "rgba(167, 139, 250, 0.15)"
_DOT_FILL = "#8b5cf6"  # accent-500


class _SparklinePoint(dict[str, object]):
    """Typing helper — :func:`_point_coordinates` returns these."""


def _point_coordinates(
    entries: list[DailyEntry],
) -> tuple[list[dict[str, object]], int]:
    """Project each ``DailyEntry`` onto SVG-space ``(x, y)`` coordinates.

    Returns ``(points, peak)``. ``points`` carries one dict per entry
    with pre-formatted ``x``, ``y``, ``date``, ``count`` and a
    ready-to-render ``tooltip``; ``peak`` is the maximum count across
    the window (``0`` when the window has zero captures).
    """
    if not entries:
        return [], 0

    peak = max((entry["count"] for entry in entries), default=0)
    plot_w = _SVG_WIDTH - 2 * _PAD_X
    plot_h = _SVG_HEIGHT - 2 * _PAD_Y
    baseline_y = _SVG_HEIGHT - _PAD_Y

    n = len(entries)
    step = 0.0 if n == 1 else plot_w / (n - 1)

    points: list[dict[str, object]] = []
    for index, entry in enumerate(entries):
        x = _PAD_X + (step * index if n > 1 else plot_w / 2)
        if peak > 0:
            ratio = entry["count"] / peak
            y = baseline_y - ratio * plot_h
        else:
            y = baseline_y
        suffix = "" if entry["count"] == 1 else "s"
        points.append(
            {
                "x": x,
                "y": y,
                "date": entry["date"],
                "count": entry["count"],
                "tooltip": f"{entry['date']} — {entry['count']} shot{suffix}",
            }
        )
    return points, peak


def _polyline_attr(points: list[dict[str, object]]) -> str:
    """Pre-format the ``points="..."`` attribute string for the polyline."""
    return " ".join(f"{p['x']:.2f},{p['y']:.2f}" for p in points)


def _band_attr(points: list[dict[str, object]]) -> str:
    """Closed polygon under the polyline — soft fill band beneath the line."""
    if not points:
        return ""
    baseline_y = _SVG_HEIGHT - _PAD_Y
    first_x = float(points[0]["x"])  # type: ignore[arg-type]
    last_x = float(points[-1]["x"])  # type: ignore[arg-type]
    line = " ".join(f"{p['x']:.2f},{p['y']:.2f}" for p in points)
    return f"{first_x:.2f},{baseline_y} {line} {last_x:.2f},{baseline_y}"


async def _tag_exists(name: str) -> bool:
    """Return ``True`` when a row with this name exists in ``tags``.

    The stats payload itself happily returns zeros for an unknown tag,
    so the route uses this existence check to distinguish "tag is
    unknown" (→ 404) from "tag exists but window is empty" (→ 200
    with all-zero cards).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM tags WHERE name = ? LIMIT 1",
            (name.strip().lower(),),
        )
        row = await cursor.fetchone()
    return row is not None


def _days_active(stats: TagStats) -> int:
    """Count non-zero days in the 30-day window."""
    return sum(1 for entry in stats["daily_timeline"] if entry["count"] > 0)


@router.get("/tag/{tag}/stats", response_class=HTMLResponse)
async def tag_stats_page(request: Request, tag: str) -> HTMLResponse:
    """Render the per-tag stats card.

    404s when ``tag`` has zero rows — either the tag is unknown or it
    has never been attached to a screenshot.
    """
    stats = await compute_tag_stats(tag)
    has_rows = stats["total"] > 0
    if not has_rows and not await _tag_exists(tag):
        raise HTTPException(status_code=404, detail=f"Tag not found: {tag}")
    if not has_rows:
        # Tag exists but never attached — spec asks for 404 on "zero rows".
        raise HTTPException(status_code=404, detail=f"No captures for tag: {tag}")

    points, peak = _point_coordinates(stats["daily_timeline"])
    polyline = _polyline_attr(points)
    band = _band_attr(points)
    days_active = _days_active(stats)
    tag_name = stats["tag"]

    return templates.TemplateResponse(
        request,
        "tag_stats.html",
        {
            "title": f"Stats: {tag_name}",
            "active_nav": "memory",
            "stats": stats,
            "tag_name": tag_name,
            "days_active": days_active,
            "points": points,
            "peak": peak,
            "polyline": polyline,
            "band": band,
            "svg_width": _SVG_WIDTH,
            "svg_height": _SVG_HEIGHT,
            "baseline_y": _SVG_HEIGHT - _PAD_Y,
            "stroke": _STROKE,
            "fill_band": _FILL_BAND,
            "dot_fill": _DOT_FILL,
            "json_url": f"/api/tag/{quote(tag_name, safe='')}/stats.json",
        },
    )


@router.get("/api/tag/{tag}/stats.json", response_class=JSONResponse)
async def tag_stats_json(tag: str) -> JSONResponse:
    """Machine-readable counterpart to :func:`tag_stats_page`."""
    stats = await compute_tag_stats(tag)
    if stats["total"] == 0 and not await _tag_exists(tag):
        raise HTTPException(status_code=404, detail=f"Tag not found: {tag}")
    if stats["total"] == 0:
        raise HTTPException(status_code=404, detail=f"No captures for tag: {tag}")
    payload: dict[str, object] = {
        "tag": stats["tag"],
        "total": stats["total"],
        "first_seen": stats["first_seen"],
        "last_seen": stats["last_seen"],
        "top_apps": [dict(row) for row in stats["top_apps"]],
        "co_occurring": [dict(row) for row in stats["co_occurring"]],
        "daily_timeline": [dict(row) for row in stats["daily_timeline"]],
    }
    return JSONResponse(payload)
