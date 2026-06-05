"""HTTP surfaces for the monthly comparison report.

Two endpoints, one source of truth:

* ``GET /stats/monthly-comparison`` — full HTML page that extends
  ``base.html`` with the four headline delta cards, the
  growth/decline columns, the mini SVG comparison bars and the
  prev/next month nav.
* ``GET /api/stats/monthly-comparison.json`` — machine-readable
  mirror of the same payload so dashboards / scripts can scrape it
  without parsing HTML.

Both endpoints accept ``?month=YYYY-MM`` to pin the report to a
specific calendar month. Omitting the parameter selects the current
local month.

This module deliberately does NOT register itself with the FastAPI
app in :mod:`app.web.main` — the task spec forbids touching
``main.py``. Wire it up with::

    from app.web.routes import monthly_comparison as monthly_comparison_routes
    app.include_router(monthly_comparison_routes.router)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.monthly_comparison import compute_comparison
from app.web.templates_engine import templates

log = get_logger("persona.monthly_comparison")

router = APIRouter(tags=["monthly-comparison"])

# Month input shape used by both endpoints. Accepts only well-formed
# ``YYYY-MM`` strings — anything else is rejected at the FastAPI layer
# before our SQL ever runs.
_MONTH_QUERY = Query(
    default=None,
    pattern=r"^\d{4}-\d{2}$",
    description="Calendar month in YYYY-MM format. Defaults to the current month.",
)


def _prev_next_months(month_iso: str) -> tuple[str, str]:
    """Compute the ``YYYY-MM`` strings immediately before and after ``month_iso``.

    Used to build the prev / next navigation links on the HTML page.
    Pure string arithmetic — no ``datetime`` round-trip — so an input
    that survived :func:`compute_comparison` parses identically here.
    """
    year_str, month_str = month_iso.split("-")
    year = int(year_str)
    month = int(month_str)
    prev_year, prev_month = (year - 1, 12) if month == 1 else (year, month - 1)
    next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
    return (
        f"{prev_year:04d}-{prev_month:02d}",
        f"{next_year:04d}-{next_month:02d}",
    )


@router.get(
    "/api/stats/monthly-comparison.json",
    response_class=JSONResponse,
)
async def monthly_comparison_json(
    month: str | None = _MONTH_QUERY,
) -> JSONResponse:
    """Return the monthly comparison payload as JSON.

    ``month`` is validated by FastAPI against ``^\\d{4}-\\d{2}$``;
    :func:`compute_comparison` then range-checks the month digits and
    raises :class:`ValueError` on out-of-range values like ``2026-13``.
    We translate that to a 400 so the JSON contract stays predictable.
    """
    try:
        payload = await compute_comparison(month)
    except ValueError as exc:
        log.warning("monthly_comparison.bad_month", month=month, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(dict(payload))


@router.get(
    "/stats/monthly-comparison",
    response_class=HTMLResponse,
)
async def monthly_comparison_page(
    request: Request,
    month: str | None = _MONTH_QUERY,
) -> HTMLResponse:
    """Render the HTML comparison page extending ``base.html``.

    Reuses the same payload as the JSON endpoint so the page and the
    machine-readable mirror can never drift. Prev / next month strings
    are computed from the *resolved* month inside the payload so the
    nav always points at neighbours of the page the user is actually
    looking at — including when ``month`` was omitted and we defaulted
    to "now".
    """
    try:
        payload = await compute_comparison(month)
    except ValueError as exc:
        log.warning("monthly_comparison.bad_month", month=month, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    this_month_iso = payload["this_month"]["month"]
    prev_iso, next_iso = _prev_next_months(this_month_iso)

    return templates.TemplateResponse(
        request,
        "monthly_comparison.html",
        {
            "title": "Месячное сравнение",
            "active_nav": "stats",
            "this_month": payload["this_month"],
            "last_month": payload["last_month"],
            "deltas": payload["deltas"],
            "top_growth": payload["top_growth"],
            "top_declines": payload["top_declines"],
            "daily_average_this": payload["daily_average_this"],
            "daily_average_last": payload["daily_average_last"],
            "prev_month_iso": prev_iso,
            "next_month_iso": next_iso,
        },
    )


__all__ = ["router"]
