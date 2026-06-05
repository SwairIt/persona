"""This-day-last-year replay — HTML timeline + JSON sibling.

Three endpoints:

* ``GET /memory/replay`` — vertical timeline of 1/2/3-year anniversaries
  anchored to *today* (UTC). Renders ``this_day_replay.html``.
* ``GET /api/memory/replay.json`` — machine-readable counterpart of the
  page above.
* ``GET /memory/replay/{ymd}`` — same timeline anchored to an explicit
  ``YYYY-MM-DD`` so a user can bookmark a specific anniversary view (eg
  their birthday) and reopen it any day of the year.

The route is intentionally *not* registered in :mod:`app.web.main` —
the task spec forbids touching ``main.py``. Wire it up later with::

    from app.web.routes import this_day_replay as this_day_replay_routes
    app.include_router(this_day_replay_routes.router)
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.this_day_replay import get_replay, get_replay_for_date
from app.web.templates_engine import templates

log = get_logger("persona.this_day_replay")

router = APIRouter(tags=["this_day_replay"])

# Default years offsets surfaced by the UI. Tuple, not list, so it's
# safe to share at module scope without worrying about accidental
# mutation by a caller upstream.
_DEFAULT_YEARS_BACK: tuple[int, int, int] = (1, 2, 3)
_DEFAULT_LIMIT_PER_YEAR = 30
# JSON endpoint accepts an override but we still cap it server-side to
# match the underlying ``get_replay`` function's own ceiling.
_MAX_LIMIT_PER_YEAR_PUBLIC = 200


def _parse_ymd(ymd: str) -> date:
    """Parse a ``YYYY-MM-DD`` path component. Raises ``HTTPException`` on bad input.

    We deliberately do **not** fall back to *today* the way some legacy
    routes do — this endpoint exists specifically so a user can bookmark
    a particular calendar day, and silently rewriting that to today
    would defeat the purpose.
    """
    try:
        return date.fromisoformat(ymd)
    except ValueError as exc:
        log.info("this_day_replay.bad_ymd", ymd=ymd)
        raise HTTPException(
            status_code=400,
            detail=f"Invalid date {ymd!r}; expected YYYY-MM-DD",
        ) from exc


@router.get("/memory/replay", response_class=HTMLResponse)
async def replay_page(request: Request) -> HTMLResponse:
    """Render the 1/2/3-years-ago replay timeline for today."""
    payload = await get_replay(
        years_back=list(_DEFAULT_YEARS_BACK),
        limit_per_year=_DEFAULT_LIMIT_PER_YEAR,
    )
    return templates.TemplateResponse(
        request,
        "this_day_replay.html",
        {
            "title": "На этот день",  # noqa: RUF001 — Russian UI title
            "active_nav": "memory",
            "payload": payload,
            "anchor_date": payload["today_iso"],
            "is_today_anchor": True,
        },
    )


@router.get("/api/memory/replay.json")
async def replay_json(
    limit_per_year: int = Query(
        _DEFAULT_LIMIT_PER_YEAR,
        ge=1,
        le=_MAX_LIMIT_PER_YEAR_PUBLIC,
    ),
) -> JSONResponse:
    """JSON sibling — same payload as the HTML page, no rendering."""
    payload = await get_replay(
        years_back=list(_DEFAULT_YEARS_BACK),
        limit_per_year=limit_per_year,
    )
    return JSONResponse(dict(payload))


@router.get("/memory/replay/{ymd}", response_class=HTMLResponse)
async def replay_for_date_page(request: Request, ymd: str) -> HTMLResponse:
    """Render the replay timeline anchored to an explicit ``YYYY-MM-DD``."""
    target = _parse_ymd(ymd)
    payload = await get_replay_for_date(
        target,
        years_back=list(_DEFAULT_YEARS_BACK),
        limit_per_year=_DEFAULT_LIMIT_PER_YEAR,
    )
    return templates.TemplateResponse(
        request,
        "this_day_replay.html",
        {
            "title": f"На этот день — {target.isoformat()}",  # noqa: RUF001 — Russian UI title
            "active_nav": "memory",
            "payload": payload,
            "anchor_date": target.isoformat(),
            "is_today_anchor": False,
        },
    )


__all__ = ["router"]
