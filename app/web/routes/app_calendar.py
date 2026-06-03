"""Per-app capture-activity calendar — HTML page and JSON endpoint.

v0.91 feature 3/3 — companion to the global yearly heatmap shipped in v0.28
(see :mod:`app.web.routes.heatmap`), narrowed to a single ``app_name`` so an
operator can answer "when do I actually open *this* app?" at a glance.

The grid, palette and percentile bucketing match the global heatmap byte-for-
byte; only the SQL aggregation is parametrised by ``app_name`` and the page
chrome calls out which app is being displayed.
"""

from __future__ import annotations

from calendar import month_abbr
from datetime import date, datetime, timedelta
from typing import TypedDict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.app_calendar")

router = APIRouter(tags=["app-calendar"])

# --- Heatmap geometry — kept in lock-step with ``app.web.routes.heatmap``
# so the per-app calendar visually matches the global one. The duplication
# is deliberate: this module is the only consumer, and importing private
# constants from a sibling route would couple two pages that may diverge
# independently in future.
_DAYS_IN_YEAR = 365
_COLS = 53
_ROWS = 7
_TILE = 12
_GAP = 2
_LEFT_PAD = 28  # room for day-of-week labels
_TOP_PAD = 18  # room for month labels

# GitHub-style palette (zinc-800 + emerald 900/700/500/400). Indexed by
# level 0..4, same as the global heatmap.
_LEVEL_FILLS: tuple[str, ...] = (
    "#27272a",
    "#064e3b",
    "#047857",
    "#10b981",
    "#34d399",
)


class _CalendarDay(TypedDict):
    date: str
    count: int
    level: int


class _CalendarPayload(TypedDict):
    app_name: str
    start_date: str
    end_date: str
    days: list[_CalendarDay]
    max_count: int
    total: int


def _percentile(sorted_values: list[int], pct: float) -> float:
    """Linear-interpolated percentile across a sorted-ascending list.

    Mirrors :func:`app.heatmap._percentile` so the per-app bucketing is
    identical to the global heatmap. ``pct`` is in ``[0, 1]``; empty input
    returns ``0.0`` so callers can use the value as a numeric threshold.
    """
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = pct * (len(sorted_values) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac


def _level_for(count: int, p33: float, p66: float, p90: float) -> int:
    """Bucket a raw day-count into a discrete ``0..4`` intensity level."""
    if count <= 0:
        return 0
    if count > p90:
        return 4
    if count > p66:
        return 3
    if count > p33:
        return 2
    return 1


async def _collect(app_name: str, anchor: date) -> _CalendarPayload:
    """Aggregate ``screenshots`` rows for ``app_name`` over 365 days.

    Returns a dense series — every day in the window appears with a
    ``count`` of ``0`` if no screenshots were captured. Raises
    :class:`HTTPException` 404 when the app has no rows at all so a
    bookmark to a renamed app surfaces a clean error rather than a blank
    grid.
    """
    start = anchor - timedelta(days=_DAYS_IN_YEAR - 1)
    start_iso = start.isoformat()
    end_iso = anchor.isoformat()

    async with get_connection() as conn:
        # Cheap existence check first — 404 on an unknown app instead of
        # rendering an empty year. Parametrised to stay safe against
        # apostrophes and other punctuation legitimately found in
        # ``app_name`` (e.g. macOS bundles like "Notes 'Lite'").
        cursor = await conn.execute(
            "SELECT 1 FROM screenshots WHERE app_name = ? LIMIT 1",
            (app_name,),
        )
        if await cursor.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"App not found: {app_name}")

        cursor = await conn.execute(
            "SELECT DATE(captured_at) AS day, COUNT(*) AS n "
            "FROM screenshots "
            "WHERE app_name = ? "
            "AND captured_at IS NOT NULL "
            "AND DATE(captured_at) BETWEEN ? AND ? "
            "GROUP BY day",
            (app_name, start_iso, end_iso),
        )
        raw_rows = await cursor.fetchall()

    counts: dict[date, int] = {}
    for row in raw_rows:
        raw_day = row["day"]
        if not raw_day:
            continue
        try:
            day = date.fromisoformat(str(raw_day)[:10])
        except ValueError:
            log.warning("app_calendar.bad_day_skipped", day=str(raw_day))
            continue
        counts[day] = counts.get(day, 0) + int(row["n"])

    non_zero_sorted: list[int] = sorted(v for v in counts.values() if v > 0)
    p33 = _percentile(non_zero_sorted, 0.33)
    p66 = _percentile(non_zero_sorted, 0.66)
    p90 = _percentile(non_zero_sorted, 0.90)

    days: list[_CalendarDay] = []
    total = 0
    max_count = 0
    for offset in range(_DAYS_IN_YEAR):
        current = start + timedelta(days=offset)
        n = counts.get(current, 0)
        max_count = max(max_count, n)
        total += n
        days.append(
            _CalendarDay(
                date=current.isoformat(),
                count=n,
                level=_level_for(n, p33, p66, p90),
            )
        )

    log.info(
        "app_calendar.computed",
        app_name=app_name,
        start=start_iso,
        end=end_iso,
        total=total,
        max_count=max_count,
        non_zero_days=len(non_zero_sorted),
        p33=p33,
        p66=p66,
        p90=p90,
    )

    return _CalendarPayload(
        app_name=app_name,
        start_date=start_iso,
        end_date=end_iso,
        days=days,
        max_count=max_count,
        total=total,
    )


def _build_grid(days: list[_CalendarDay]) -> list[list[_CalendarDay | None]]:
    """Bucket the dense day list into a column-major 53-by-7 grid.

    Same shape as the global heatmap — row 0 is Monday, the first column
    may have leading ``None`` cells so the start date lines up with its
    true weekday, and trailing cells in the final column are padded so
    iteration stays uniform.
    """
    if not days:
        return [[None] * _ROWS for _ in range(_COLS)]

    first = date.fromisoformat(days[0]["date"])
    lead = first.weekday()  # Monday=0..Sunday=6
    cells: list[_CalendarDay | None] = [None] * lead
    cells.extend(days)
    target = _COLS * _ROWS
    if len(cells) < target:
        cells.extend([None] * (target - len(cells)))
    else:
        cells = cells[:target]

    grid: list[list[_CalendarDay | None]] = []
    for col in range(_COLS):
        column = cells[col * _ROWS : (col + 1) * _ROWS]
        grid.append(column)
    return grid


def _month_labels(grid: list[list[_CalendarDay | None]]) -> list[dict[str, object]]:
    """Pick one label per column where a new month starts in the column."""
    labels: list[dict[str, object]] = []
    last_month: int | None = None
    for col_index, column in enumerate(grid):
        anchor: _CalendarDay | None = None
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


@router.get("/apps/{app_name}/calendar", response_class=HTMLResponse)
async def app_calendar_page(request: Request, app_name: str) -> HTMLResponse:
    """Render the per-app capture-activity calendar as a Tailwind page."""
    anchor = datetime.now().astimezone().date()
    payload = await _collect(app_name, anchor)
    grid = _build_grid(payload["days"])
    month_labels = _month_labels(grid)

    svg_width = _LEFT_PAD + _COLS * (_TILE + _GAP)
    svg_height = _TOP_PAD + _ROWS * (_TILE + _GAP)

    return templates.TemplateResponse(
        request,
        "app_calendar.html",
        {
            "title": f"Calendar · {app_name}",
            "active_nav": "stats",
            "app_name": app_name,
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


@router.get("/api/apps/{app_name}/calendar.json", response_class=JSONResponse)
async def app_calendar_json(app_name: str) -> JSONResponse:
    """Machine-readable counterpart of the HTML calendar page."""
    anchor = datetime.now().astimezone().date()
    payload = await _collect(app_name, anchor)
    return JSONResponse(dict(payload))
