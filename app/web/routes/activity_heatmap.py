"""365-day activity heatmap — HTML page and JSON endpoint.

Renders the yearly capture-activity overview at ``/activity`` plus a
machine-readable mirror at ``/api/activity/year.json``. The two share a
single :func:`app.activity_heatmap.build_year_heatmap` call so the page
and the JSON can never drift.
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.activity_heatmap import ActivityDay, build_year_heatmap
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.activity_heatmap")

router = APIRouter(tags=["activity"])

# 7-row by 53-column GitHub-style grid. Tiles are 11px squares with a
# 2px gap as per the spec; the cell pitch is therefore 13px.
_ROWS = 7
_COLS = 53
_TILE = 11
_GAP = 2

# Tier → colour, indexed by tier 0..4. Hex values are the Tailwind
# zinc/emerald palette (the same scale the existing /heatmap route
# pulls in) so the page stays visually consistent with the rest of the
# stats area when rendered without Tailwind CDN.
_TIER_FILLS: tuple[str, ...] = (
    "#27272a",  # zinc-800   — no activity
    "#064e3b",  # emerald-900 — q25 floor
    "#047857",  # emerald-700 — q50
    "#10b981",  # emerald-500 — q75
    "#6ee7b7",  # emerald-300 — peak
)


def _build_grid(days: list[ActivityDay]) -> list[list[ActivityDay | None]]:
    """Bucket the dense day series into a 53-column by 7-row grid.

    Each column is one ISO week, row 0 = Monday through row 6 = Sunday.
    The first column is left-padded with ``None`` so the start day lines
    up with its real weekday; trailing slots in the last column are also
    ``None`` so the template can iterate uniformly without bounds checks.
    """
    if not days:
        return [[None] * _ROWS for _ in range(_COLS)]

    first = date.fromisoformat(days[0]["date"])
    lead = first.weekday()  # Monday=0..Sunday=6
    cells: list[ActivityDay | None] = [None] * lead
    cells.extend(days)
    target = _COLS * _ROWS
    if len(cells) < target:
        cells.extend([None] * (target - len(cells)))
    else:
        cells = cells[:target]

    grid: list[list[ActivityDay | None]] = []
    for col in range(_COLS):
        column = cells[col * _ROWS : (col + 1) * _ROWS]
        grid.append(column)
    return grid


@router.get("/activity", response_class=HTMLResponse)
async def activity_page(request: Request) -> HTMLResponse:
    """Render the yearly activity heatmap at ``/activity``."""
    payload = await build_year_heatmap()
    grid = _build_grid(payload["days"])

    svg_width = _COLS * (_TILE + _GAP)
    svg_height = _ROWS * (_TILE + _GAP)

    return templates.TemplateResponse(
        request,
        "activity_heatmap.html",
        {
            "title": "Активность",
            "active_nav": "stats",
            "grid": grid,
            "total_shots": payload["total_shots"],
            "total_days_with_activity": payload["total_days_with_activity"],
            "max_day_count": payload["max_day_count"],
            "streak_current": payload["streak_current"],
            "streak_longest": payload["streak_longest"],
            "tier_fills": _TIER_FILLS,
            "tile": _TILE,
            "gap": _GAP,
            "rows": _ROWS,
            "cols": _COLS,
            "svg_width": svg_width,
            "svg_height": svg_height,
        },
    )


@router.get("/api/activity/year.json", response_class=JSONResponse)
async def activity_year_json() -> JSONResponse:
    """Return the same payload as the HTML page, JSON-encoded."""
    payload = await build_year_heatmap()
    return JSONResponse(dict(payload))
