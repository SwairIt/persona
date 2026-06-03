"""HTML + JSON endpoints for the multi-day diff view (Persona v0.71).

Two surfaces share a single :func:`app.multi_day_diff.compare_days`
backend:

* ``GET /stats/diff?a=YYYY-MM-DD&b=YYYY-MM-DD`` — Tailwind side-by-side
  comparison page (template :file:`multi_day_diff.html`).
* ``GET /api/diff-days.json`` — same payload as a JSON document for
  programmatic consumers and for the in-page client to live-refresh
  without a server-side template render.

Both endpoints validate the date query params here and return ``400``
for malformed input, so the helper layer can stay strict about types.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.multi_day_diff import compare_days
from app.web.templates_engine import templates

log = get_logger("persona.multi_day_diff")

router = APIRouter(tags=["stats"])


def _default_days() -> tuple[str, str]:
    """Return ``(yesterday, today)`` in the local timezone as ISO dates.

    Used when the caller omits ``a`` / ``b`` query params — gives the
    page a sane "yesterday vs today" landing view instead of erroring.
    Going via :func:`date.fromordinal` handles month / year rollover
    cleanly without a ``timedelta`` import.
    """
    today = datetime.now().astimezone().date()
    yesterday = date.fromordinal(today.toordinal() - 1)
    return yesterday.isoformat(), today.isoformat()


def _resolve_days(a: str | None, b: str | None) -> tuple[str, str]:
    """Fall back to ``(yesterday, today)`` when either param is missing."""
    default_a, default_b = _default_days()
    return (a or default_a, b or default_b)


async def _compute_or_400(a: str, b: str) -> dict[str, Any]:
    """Run the comparison, translating bad-date ``ValueError`` to HTTP 400."""
    try:
        return await compare_days(a, b)
    except ValueError as exc:
        log.info("multi_day_diff.bad_date", a=a, b=b, error=str(exc))
        raise HTTPException(
            status_code=400,
            detail="Both 'a' and 'b' must be YYYY-MM-DD dates",
        ) from exc


@router.get("/stats/diff", response_class=HTMLResponse)
async def multi_day_diff_page(
    request: Request,
    a: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    b: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
) -> HTMLResponse:
    """Render the two-column comparison page for days ``a`` and ``b``."""
    day_a, day_b = _resolve_days(a, b)
    payload = await _compute_or_400(day_a, day_b)

    return templates.TemplateResponse(
        request,
        "multi_day_diff.html",
        {
            "title": f"Diff {payload['day_a']} vs {payload['day_b']}",
            "active_nav": "stats",
            "diff": payload,
        },
    )


@router.get("/api/diff-days.json", response_class=JSONResponse)
async def multi_day_diff_json(
    a: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
    b: str | None = Query(default=None, description="ISO date YYYY-MM-DD"),
) -> JSONResponse:
    """Return the same comparison payload as JSON for API consumers."""
    day_a, day_b = _resolve_days(a, b)
    payload = await _compute_or_400(day_a, day_b)
    return JSONResponse(payload)
