"""HTTP routes for the Spotify-style yearly Wrapped page.

Three endpoints:

* ``GET /wrapped`` — renders the current calendar year.
* ``GET /wrapped/{year}`` — renders a specific year (4-digit ``int``).
* ``GET /api/wrapped/{year}.json`` — machine-readable twin of the page,
  returning the same payload as a JSON document.

The HTML twin renders :file:`yearly_wrapped.html`; the JSON twin re-uses
the exact dict from :func:`app.yearly_wrapped.compute_yearly_wrapped`
so the two surfaces never drift.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.web.templates_engine import templates
from app.yearly_wrapped import compute_yearly_wrapped

log = get_logger("persona.yearly_wrapped.routes")

router = APIRouter(tags=["yearly_wrapped"])

# Sanity bound for the ``{year}`` path parameter. FastAPI hands us an
# ``int`` automatically (so non-numeric input becomes a 422), but a
# pathological negative or far-future value would still hit SQLite and
# scan nothing forever. We accept the full Gregorian range that SQLite's
# ``DATE()`` function understands without complaint.
_YEAR_MIN = 1970
_YEAR_MAX = 9999


def _current_year() -> int:
    """Return the current UTC year as an ``int``.

    Anchored on UTC so two operators in different timezones see the same
    "current year" for the ``/wrapped`` shortcut around midnight on
    Dec 31 / Jan 1 — the SQL bounds in :mod:`app.yearly_wrapped` are
    also UTC, so this keeps the two ends in lockstep.
    """
    return datetime.now(tz=UTC).year


def _validate_year(year: int) -> int:
    """Clamp / reject out-of-range years before they hit SQL."""
    if year < _YEAR_MIN or year > _YEAR_MAX:
        log.warning("yearly_wrapped.year_out_of_range", year=year)
        raise HTTPException(status_code=400, detail="year out of range")
    return year


def _render_page(request: Request, year: int, payload: dict[str, Any]) -> HTMLResponse:
    """Shared HTML render path used by both ``/wrapped`` and ``/wrapped/{year}``."""
    return templates.TemplateResponse(
        request,
        "yearly_wrapped.html",
        {
            "title": f"Wrapped {year}",
            "active_nav": "stats",
            "year": year,
            "wrapped": payload,
        },
    )


@router.get("/wrapped", response_class=HTMLResponse)
async def wrapped_current(request: Request) -> HTMLResponse:
    """Render the Wrapped page for the current UTC year."""
    year = _current_year()
    payload = await compute_yearly_wrapped(year)
    log.info("yearly_wrapped.page", year=year, mode="current")
    return _render_page(request, year, payload)


@router.get("/wrapped/{year}", response_class=HTMLResponse)
async def wrapped_for_year(request: Request, year: int) -> HTMLResponse:
    """Render the Wrapped page for an explicit year."""
    year = _validate_year(year)
    payload = await compute_yearly_wrapped(year)
    log.info("yearly_wrapped.page", year=year, mode="explicit")
    return _render_page(request, year, payload)


@router.get("/api/wrapped/{year}.json", response_class=JSONResponse)
async def wrapped_json(year: int) -> JSONResponse:
    """Return the Wrapped payload for ``year`` as JSON."""
    year = _validate_year(year)
    payload = await compute_yearly_wrapped(year)
    log.info("yearly_wrapped.json", year=year)
    return JSONResponse(payload)


__all__ = ["router"]
