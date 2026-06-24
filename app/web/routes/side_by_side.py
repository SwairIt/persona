"""Side-by-side screenshot comparison — two shots rendered full-size next to each other.

Distinct from :mod:`app.web.routes.diff_slider` (v0.33, single image with a
draggable reveal handle) and :mod:`app.web.routes.diff_picker` (the form that
selects two ids): this view drops both screenshots into a responsive two-column
grid so the user can scan them in parallel without losing detail behind a
slider mask. A metadata strip per pane keeps app / captured_at / dimensions
visible while scrolling, and the URL itself (``/compare/{id_a}/{id_b}``) is
shareable — there is no client-only state.

The template also wires arrow keys to walk through the "shots-of-the-day"
series: pressing ← / → swaps both ids for the deterministic shot-of-the-day
ids one calendar day earlier / later, so the URL after navigation is still a
plain shareable permalink.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot

if TYPE_CHECKING:
    import aiosqlite

    from app.storage.models import Screenshot

from app.web.templates_engine import templates

log = get_logger("persona.side_by_side")

router = APIRouter(tags=["analysis"])

# Mirrors the constants in :mod:`app.shot_of_day` so the arrow-key navigation
# walks the same deterministic series. Kept as private module-level constants
# (not imported from ``shot_of_day``) so a future tweak to that algorithm does
# not silently change permalink behaviour of this comparison view.
_CANDIDATE_WINDOW_DAYS = 90
_CANDIDATE_LIMIT = 5000

# How far back / forward the arrow-key navigation is allowed to step from
# today. Keeping a hard ceiling avoids unbounded date arithmetic if a script
# spams the JSON endpoint with a huge ``offset`` query.
_OFFSET_LIMIT_DAYS = 365


def _stable_seed(iso_date: str) -> int:
    """Return a stable 64-bit unsigned int derived from ``iso_date``.

    Same construction as :func:`app.shot_of_day._stable_seed` — duplicated
    rather than imported so the private symbol can move without breaking the
    side-by-side permalink contract.
    """
    digest = hashlib.sha256(iso_date.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


async def _shot_of_day_id_for(
    conn: aiosqlite.Connection,
    target_day: date,
) -> int | None:
    """Return the deterministic shot-of-the-day id for ``target_day``.

    Picks from the same ``_CANDIDATE_WINDOW_DAYS`` rolling window the live
    shot-of-the-day endpoint uses, but the *seed* is derived from the
    requested date string rather than "today" so previous / next days yield
    a stable pair. Returns ``None`` when no candidates exist in the window
    (e.g. brand-new install with no captures yet).
    """
    cursor = await conn.execute(
        "SELECT id FROM screenshots "
        "WHERE captured_at >= datetime('now', ?) "
        "ORDER BY id "
        "LIMIT ?",
        (f"-{_CANDIDATE_WINDOW_DAYS} days", _CANDIDATE_LIMIT),
    )
    rows = await cursor.fetchall()
    candidate_ids: list[int] = [int(row["id"]) for row in rows]
    if not candidate_ids:
        return None
    seed = _stable_seed(target_day.isoformat())
    return candidate_ids[seed % len(candidate_ids)]


def _clamp_offset(offset: int) -> int:
    """Clamp ``offset`` into ``[-_OFFSET_LIMIT_DAYS, _OFFSET_LIMIT_DAYS]``."""
    if offset < -_OFFSET_LIMIT_DAYS:
        return -_OFFSET_LIMIT_DAYS
    if offset > _OFFSET_LIMIT_DAYS:
        return _OFFSET_LIMIT_DAYS
    return offset


@router.get("/compare/{id_a}/{id_b}", response_class=HTMLResponse)
async def side_by_side_page(
    request: Request,
    _user: Annotated[SessionRecord, Depends(current_user_required)],
    id_a: int,
    id_b: int,
) -> HTMLResponse:
    """Render the side-by-side comparison for ``id_a`` and ``id_b``.

    Returns 404 when either id is missing — keep both required so the page
    never half-renders with one broken ``<img>``. The URL is the only piece
    of shareable state; there is no cookie or query string.
    """
    async with get_connection() as conn:
        shot_a = await get_screenshot(conn, id_a)
        shot_b = await get_screenshot(conn, id_b)

    if shot_a is None or shot_b is None:
        missing = [i for i, s in ((id_a, shot_a), (id_b, shot_b)) if s is None]
        log.info("side_by_side.not_found", missing=missing)
        raise HTTPException(status_code=404, detail="Screenshot not found")

    log.info("side_by_side.render", id_a=id_a, id_b=id_b)
    return _render(request, shot_a, shot_b)


@router.get("/api/compare/shots-of-day.json", response_class=JSONResponse)
async def side_by_side_shots_of_day(
    _user: Annotated[SessionRecord, Depends(current_user_required)],
    offset: int = Query(default=0, description="Day offset from today; arrow keys step by ±1."),
) -> JSONResponse:
    """Return the shot-of-day id pair (``offset`` and ``offset - 1``) as JSON.

    Used by the template's arrow-key handler: pressing ``→`` requests the
    pair one day forward, pressing ``←`` requests the pair one day backward.
    Returns ``{"id_a": null, "id_b": null}`` when the underlying candidate
    pool is empty — the client treats that as "no neighbour, stay put".
    """
    clamped = _clamp_offset(offset)
    today = datetime.now(tz=UTC).date()
    day_a = today + timedelta(days=clamped - 1)
    day_b = today + timedelta(days=clamped)

    async with get_connection() as conn:
        id_a = await _shot_of_day_id_for(conn, day_a)
        id_b = await _shot_of_day_id_for(conn, day_b)

    log.info(
        "side_by_side.shots_of_day",
        offset=clamped,
        day_a=day_a.isoformat(),
        day_b=day_b.isoformat(),
        id_a=id_a,
        id_b=id_b,
    )
    return JSONResponse({"offset": clamped, "id_a": id_a, "id_b": id_b})


def _render(request: Request, shot_a: Screenshot, shot_b: Screenshot) -> HTMLResponse:
    """Shared TemplateResponse so any future entry points stay in lockstep."""
    return templates.TemplateResponse(
        request,
        "side_by_side.html",
        {
            "title": f"Compare #{shot_a.id} vs #{shot_b.id}",
            "active_nav": "timeline",
            "shot_a": shot_a,
            "shot_b": shot_b,
        },
    )
