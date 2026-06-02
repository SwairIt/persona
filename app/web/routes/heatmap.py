"""Yearly capture-activity heatmap — HTML page and JSON endpoint."""

from __future__ import annotations

from calendar import month_abbr
from datetime import date

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.heatmap import HeatmapDay, yearly_heatmap
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.heatmap")

router = APIRouter(tags=["heatmap"])

# GitHub-style palette — pulled from the Tailwind classes already in use
# elsewhere in the app (zinc/emerald scales loaded from the CDN config).
# Indexed by level 0..4.
_LEVEL_FILLS: tuple[str, ...] = (
    "#27272a",  # zinc-800 — empty
    "#064e3b",  # emerald-900 — quiet
    "#047857",  # emerald-700 — modest
    "#10b981",  # emerald-500 — busy
    "#34d399",  # emerald-400 — peak
)

# 53 columns by 7 rows visual grid.
_COLS = 53
_ROWS = 7
_TILE = 12
_GAP = 2
_LEFT_PAD = 28  # room for day-of-week labels
_TOP_PAD = 18  # room for month labels


def _build_grid(days: list[HeatmapDay]) -> list[list[HeatmapDay | None]]:
    """Bucket the dense day list into a column-major 53-by-7 grid.

    Each column represents one ISO week (Mon..Sun, row 0 = Monday).  The
    first column may have leading empty rows (``None``) so the start_date
    lines up with its true weekday; trailing empty cells in the final column
    are filled with ``None`` for the same reason.
    """
    if not days:
        return [[None] * _ROWS for _ in range(_COLS)]

    first = date.fromisoformat(days[0]["date"])
    lead = first.weekday()  # Monday=0..Sunday=6
    cells: list[HeatmapDay | None] = [None] * lead
    cells.extend(days)
    # Pad to a full grid of COLS*ROWS so iteration is uniform.
    target = _COLS * _ROWS
    if len(cells) < target:
        cells.extend([None] * (target - len(cells)))
    else:
        cells = cells[:target]

    grid: list[list[HeatmapDay | None]] = []
    for col in range(_COLS):
        column = cells[col * _ROWS : (col + 1) * _ROWS]
        grid.append(column)
    return grid


def _month_labels(grid: list[list[HeatmapDay | None]]) -> list[dict[str, object]]:
    """Pick one label per column where a new month starts in row 0.

    Returns ``[{"col": int, "x": int, "label": str}]`` so the template can
    drop them straight into ``<text>`` elements.
    """
    labels: list[dict[str, object]] = []
    last_month: int | None = None
    for col_index, column in enumerate(grid):
        # Find the first real day in this column to decide which month
        # this column "belongs to" — prefer row 0 (Monday) when present.
        anchor: HeatmapDay | None = None
        for cell in column:
            if cell is not None:
                anchor = cell
                break
        if anchor is None:
            continue
        month = date.fromisoformat(anchor["date"]).month
        if month != last_month:
            last_month = month
            x = _LEFT_PAD + col_index * (_TILE + _GAP)
            labels.append({"col": col_index, "x": x, "label": month_abbr[month]})
    return labels


@router.get("/heatmap", response_class=HTMLResponse)
async def heatmap_page(request: Request) -> HTMLResponse:
    payload = await yearly_heatmap()
    grid = _build_grid(payload["days"])
    month_labels = _month_labels(grid)

    svg_width = _LEFT_PAD + _COLS * (_TILE + _GAP)
    svg_height = _TOP_PAD + _ROWS * (_TILE + _GAP)

    return templates.TemplateResponse(
        request,
        "heatmap.html",
        {
            "title": "Heatmap",
            "active_nav": "stats",
            "start_date": payload["start_date"],
            "end_date": payload["end_date"],
            "total": payload["total"],
            "max_count": payload["max_count"],
            "grid": grid,
            "month_labels": month_labels,
            "level_fills": _LEVEL_FILLS,
            "tile": _TILE,
            "gap": _GAP,
            "left_pad": _LEFT_PAD,
            "top_pad": _TOP_PAD,
            "svg_width": svg_width,
            "svg_height": svg_height,
            "rows": _ROWS,
            "cols": _COLS,
        },
    )


@router.get("/api/heatmap.json", response_class=JSONResponse)
async def heatmap_json() -> JSONResponse:
    payload = await yearly_heatmap()
    return JSONResponse(dict(payload))
