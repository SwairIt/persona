"""Per-tag day-by-day trend — HTML page with SVG sparkline and JSON endpoint.

The sparkline is a pure 320x60 ``<polyline>`` — no JavaScript, no canvas,
no client-side chart library. Hover the polyline points (rendered as
small SVG circles) for native ``<title>`` tooltips.
"""

from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.tag_trends import TagTrendEntry, tag_trend
from app.web.templates_engine import templates

log = get_logger("persona.tag_trends")

router = APIRouter(tags=["tag-trends"])

# SVG geometry — 320x60 polyline as required, no JS / no animation.
_SVG_WIDTH = 320
_SVG_HEIGHT = 60
_PAD_X = 4
_PAD_Y = 6
_STROKE = "#a78bfa"  # accent-400
_FILL_BAND = "rgba(167, 139, 250, 0.15)"
_DOT_FILL = "#8b5cf6"  # accent-500

_MIN_DAYS = 1
_MAX_DAYS = 3650


def _clamp_window(days: int) -> int:
    """Clamp ``days`` to the safe UI range. Mirrors the model-side guard."""
    if days < _MIN_DAYS:
        return _MIN_DAYS
    if days > _MAX_DAYS:
        return _MAX_DAYS
    return days


def _point_coordinates(
    entries: list[TagTrendEntry],
) -> tuple[list[dict[str, object]], int]:
    """Compute SVG-space ``(x, y)`` for each entry and the peak count.

    Returns a tuple of:

    * a list of dicts (one per entry) with pre-formatted ``x``, ``y``,
      tooltip text and the raw fields the template needs;
    * the peak count across the window (``0`` when empty).
    """
    if not entries:
        return [], 0

    peak = max((entry["count"] for entry in entries), default=0)
    plot_w = _SVG_WIDTH - 2 * _PAD_X
    plot_h = _SVG_HEIGHT - 2 * _PAD_Y
    baseline_y = _SVG_HEIGHT - _PAD_Y

    n = len(entries)
    # Spread evenly; a single-entry window pins the point to the centre.
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
    """A closed polygon under the sparkline for the soft fill band."""
    if not points:
        return ""
    baseline_y = _SVG_HEIGHT - _PAD_Y
    first_x = float(points[0]["x"])  # type: ignore[arg-type]
    last_x = float(points[-1]["x"])  # type: ignore[arg-type]
    line = " ".join(f"{p['x']:.2f},{p['y']:.2f}" for p in points)
    return f"{first_x:.2f},{baseline_y} {line} {last_x:.2f},{baseline_y}"


async def _lookup_tag(name: str) -> dict[str, object] | None:
    """Fetch the tag row + screenshot count by case-insensitive name."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT t.id AS id, t.name AS name, t.color AS color,
                   (SELECT COUNT(*) FROM screenshot_tags st WHERE st.tag_id = t.id)
                       AS shot_count
            FROM tags t
            WHERE t.name = ?
            """,
            (name.strip().lower(),),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "color": row["color"],
        "shot_count": int(row["shot_count"] or 0),
    }


@router.get("/tags/{tag}/trend", response_class=HTMLResponse)
async def tag_trend_page(
    request: Request,
    tag: str,
    days: int = Query(default=30, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the per-tag trend page with a 320x60 SVG sparkline."""
    window = _clamp_window(days)
    tag_row = await _lookup_tag(tag)
    if tag_row is None:
        raise HTTPException(status_code=404, detail=f"Tag not found: {tag}")

    entries = await tag_trend(tag, days=window)
    points, peak = _point_coordinates(entries)
    total = sum(entry["count"] for entry in entries)
    polyline = _polyline_attr(points)
    band = _band_attr(points)
    tag_name = str(tag_row["name"])
    search_q = f"tag:{tag_name}"

    return templates.TemplateResponse(
        request,
        "tag_trend.html",
        {
            "title": f"Trend · {tag_row['name']}",
            "active_nav": "tags",
            "tag": tag_row,
            "days": window,
            "entries": entries,
            "points": points,
            "polyline": polyline,
            "band": band,
            "total": total,
            "peak": peak,
            "svg_width": _SVG_WIDTH,
            "svg_height": _SVG_HEIGHT,
            "baseline_y": _SVG_HEIGHT - _PAD_Y,
            "stroke": _STROKE,
            "fill_band": _FILL_BAND,
            "dot_fill": _DOT_FILL,
            "search_url": f"/search?q={quote(search_q)}",
            "json_url": f"/api/tags/{quote(tag_name, safe='')}/trend.json?days={window}",
        },
    )


@router.get("/api/tags/{tag}/trend.json", response_class=JSONResponse)
async def tag_trend_json(
    tag: str,
    days: int = Query(default=30, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> JSONResponse:
    """Machine-readable counterpart to :func:`tag_trend_page`."""
    window = _clamp_window(days)
    tag_row = await _lookup_tag(tag)
    if tag_row is None:
        raise HTTPException(status_code=404, detail=f"Tag not found: {tag}")
    entries = await tag_trend(tag, days=window)
    total = sum(entry["count"] for entry in entries)
    peak = max((entry["count"] for entry in entries), default=0)
    return JSONResponse(
        {
            "tag": tag_row["name"],
            "days": window,
            "total": total,
            "peak": peak,
            "entries": [dict(entry) for entry in entries],
        }
    )
