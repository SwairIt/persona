"""HTTP surfaces for the today-vs-7-day-average widget.

Two endpoints, one source of truth:

* ``GET /api/stats/today-vs-average.json`` — JSON mirror of
  :func:`app.today_vs_average.compute_today_vs_average`. Machine-readable
  shape: ``{today_iso, metrics: [...]}``.
* ``GET /widget/today-vs-average`` — small HTML fragment (no
  ``base.html`` extends) rendered from ``_today_vs_average.html``.
  Designed to be pulled into ``/stats`` via HTMX
  (``hx-get="/widget/today-vs-average" hx-trigger="load"``).

The HTML twin reuses the exact same payload — no second SQL pass —
so the widget and the JSON can never drift.

This module deliberately does NOT register itself with the FastAPI
app in :mod:`app.web.main` — the task spec forbids touching
``main.py``. Wire it up with::

    from app.web.routes import today_vs_average as today_vs_average_routes
    app.include_router(today_vs_average_routes.router)
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.today_vs_average import compute_today_vs_average
from app.web.templates_engine import templates

log = get_logger("persona.today_vs_average")

router = APIRouter(tags=["today-vs-average"])


@router.get(
    "/api/stats/today-vs-average.json",
    response_class=JSONResponse,
)
async def today_vs_average_json() -> JSONResponse:
    """Return today's metrics + 7-day-avg deltas as JSON.

    The payload is already a plain ``dict``-shaped :class:`TypedDict`,
    so we cast through :class:`dict` to make the JSON serialiser happy
    without copying any actual values.
    """
    payload = await compute_today_vs_average()
    return JSONResponse(dict(payload))


@router.get(
    "/widget/today-vs-average",
    response_class=HTMLResponse,
)
async def today_vs_average_widget(request: Request) -> HTMLResponse:
    """Render the HTMX-embeddable HTML fragment.

    Returns ``_today_vs_average.html`` directly — no ``base.html``
    extends — so an ``hx-swap="innerHTML"`` (or the implicit default)
    on the calling element drops the fragment in place without a
    second navigation chrome layer.
    """
    payload = await compute_today_vs_average()
    return templates.TemplateResponse(
        request,
        "_today_vs_average.html",
        {
            "today_iso": payload["today_iso"],
            "metrics": payload["metrics"],
        },
    )
