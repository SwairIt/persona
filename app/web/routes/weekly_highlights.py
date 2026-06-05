"""Weekly highlights — HTML page + JSON API + delete endpoint (v1.46).

Renders the curated 5-7 picks per ISO week produced by
:func:`app.llm.weekly_highlights.generate_highlights` on
``/memory/highlights`` and exposes the per-week list as JSON at
``/api/highlights/week/{week_start}.json``. A small ``POST
/api/highlights/{id}/delete`` endpoint removes a single curated pick
so the user can hide noise without re-running the LLM.

This module deliberately does NOT register itself with the FastAPI
app in :mod:`app.web.main`; it is wired by a follow-up patch with::

    from app.web.routes import weekly_highlights as weekly_highlights_routes
    app.include_router(weekly_highlights_routes.router)
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Final

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.web.weekly_highlights")

router = APIRouter(tags=["highlights"])

_DEFAULT_WEEKS: Final[int] = 12
_MAX_WEEKS: Final[int] = 52


def _row_to_pick(row: Any) -> dict[str, Any]:
    """Normalise a ``weekly_highlight`` row to a JSON-ready dict."""
    return {
        "id": int(row["id"]),
        "week_start": str(row["week_start"]),
        "rank": int(row["rank"]),
        "source_kind": str(row["source_kind"]),
        "source_id": int(row["source_id"]),
        "title": str(row["title"]),
        "reason": str(row["reason"]),
        "created_at": str(row["created_at"]) if row["created_at"] is not None else None,
    }


async def _fetch_recent_weeks(weeks: int) -> list[dict[str, Any]]:
    """Return the most recent ``weeks`` weeks worth of picks, grouped.

    The renderer wants a week-by-week list of cards, so we group the
    flat ``weekly_highlight`` rows by ``week_start`` while keeping the
    "newest week first" outer order and "rank ascending" inner order.
    """
    bounded = max(1, min(weeks, _MAX_WEEKS))
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, week_start, rank, source_kind, source_id, "
            "title, reason, created_at "
            "FROM weekly_highlight "
            "WHERE week_start IN ("
            "    SELECT DISTINCT week_start FROM weekly_highlight "
            "    ORDER BY week_start DESC LIMIT ?"
            ") "
            "ORDER BY week_start DESC, rank ASC",
            (bounded,),
        )
        rows = await cursor.fetchall()

    grouped: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for row in rows:
        pick = _row_to_pick(row)
        grouped.setdefault(pick["week_start"], []).append(pick)

    return [{"week_start": week, "picks": picks} for week, picks in grouped.items()]


async def _fetch_one_week(week_start_iso: str) -> list[dict[str, Any]]:
    """Return all picks for a single ``week_start``, rank-ordered."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, week_start, rank, source_kind, source_id, "
            "title, reason, created_at "
            "FROM weekly_highlight "
            "WHERE week_start = ? "
            "ORDER BY rank ASC",
            (week_start_iso,),
        )
        rows = await cursor.fetchall()
    return [_row_to_pick(row) for row in rows]


@router.get("/memory/highlights", response_class=HTMLResponse)
async def highlights_page(
    request: Request,
    weeks: int = Query(default=_DEFAULT_WEEKS, ge=1, le=_MAX_WEEKS),
) -> HTMLResponse:
    """Render the week-by-week list of curated highlight cards."""
    weeks_data = await _fetch_recent_weeks(weeks)
    log.info("highlights.page", weeks=len(weeks_data), limit=weeks)
    return templates.TemplateResponse(
        request,
        "weekly_highlights.html",
        {
            "title": "Highlights",
            "active_nav": "memory",
            "weeks_data": weeks_data,
            "weeks_limit": weeks,
        },
    )


@router.get("/api/highlights/week/{week_start_iso}.json", response_class=JSONResponse)
async def highlights_week_json(
    week_start_iso: str = Path(..., min_length=10, max_length=10),
) -> JSONResponse:
    """Return the curated picks for a single ISO week as JSON.

    ``week_start_iso`` must be ``YYYY-MM-DD`` (the Monday of the target
    week). We do not parse it into a ``date`` here on purpose — the
    table stores the string verbatim, so an exact-match query is the
    cheapest read.
    """
    picks = await _fetch_one_week(week_start_iso)
    return JSONResponse(
        {
            "week_start": week_start_iso,
            "picks": picks,
            "count": len(picks),
        }
    )


@router.post("/api/highlights/{pick_id}/delete", response_class=JSONResponse)
async def highlights_delete(
    pick_id: int = Path(..., ge=1),
) -> JSONResponse:
    """Remove a single curated pick by ``id``.

    Returns ``{"deleted": true}`` on success or ``{"deleted": false}``
    when no row matched (the pick was already gone). Both responses
    use HTTP 200 — the operation is idempotent from the caller's POV.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM weekly_highlight WHERE id = ?",
            (int(pick_id),),
        )
        deleted = (cursor.rowcount or 0) > 0
        await conn.commit()
    log.info("highlights.delete", pick_id=int(pick_id), deleted=deleted)
    return JSONResponse({"deleted": deleted, "id": int(pick_id)})


__all__ = ["router"]
