"""24-hour active-window minute timeline — HTML page, widget, JSON endpoint.

Renders the full-page sparkline at ``/stats/active-window``, a
standalone HTMX-embeddable SVG strip at
``/widget/active-window-sparkline``, and a machine-readable mirror at
``/api/stats/active-window.json``. All three share a single
:func:`app.active_window_timeline.build_24h_active` call so they can
never drift apart.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.active_window_timeline import build_24h_active
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.active_window_timeline")

router = APIRouter(tags=["stats"])

# Full-page SVG geometry — 1440 minute-wide rects at 1px each. The
# rendered strip is 60px tall per the spec; the legend underneath uses
# the same colour swatches sized as compact 12px squares.
_FULL_WIDTH = 1440
_FULL_HEIGHT = 60

# Sparkline (HTMX-embeddable widget) geometry — same minute-count but
# half-height so dashboards can stack it tightly above other cards.
_SPARK_WIDTH = 1440
_SPARK_HEIGHT = 24


def _common_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Shared template context for the full page and the sparkline partial.

    Keeps the two templates iterating over the same ``runs`` list rather
    than the dense ``minutes`` series — 1440 ``<rect>`` nodes balloon
    the HTML size for no visual win on the strip view, while the run
    list typically lands in the low hundreds for a normal day.
    """
    return {
        "runs": payload["runs"],
        "minutes": payload["minutes"],
        "top_apps": payload["top_apps"],
        "anchor_iso": payload["anchor_iso"],
        "start_iso": payload["start_iso"],
    }


@router.get("/stats/active-window", response_class=HTMLResponse)
async def active_window_page(request: Request) -> HTMLResponse:
    """Render the full-page 24h active-window timeline."""
    payload = await build_24h_active()
    context = _common_context(dict(payload))
    context.update(
        {
            "title": "24-часовая активность",
            "active_nav": "stats",
            "svg_width": _FULL_WIDTH,
            "svg_height": _FULL_HEIGHT,
        }
    )
    return templates.TemplateResponse(request, "active_window_24h.html", context)


@router.get("/widget/active-window-sparkline", response_class=HTMLResponse)
async def active_window_sparkline(request: Request) -> HTMLResponse:
    """Return the standalone sparkline fragment for HTMX-embed."""
    payload = await build_24h_active()
    context = _common_context(dict(payload))
    context.update(
        {
            "svg_width": _SPARK_WIDTH,
            "svg_height": _SPARK_HEIGHT,
        }
    )
    return templates.TemplateResponse(
        request,
        "_active_window_sparkline.html",
        context,
    )


@router.get("/api/stats/active-window.json", response_class=JSONResponse)
async def active_window_json() -> JSONResponse:
    """Return the same payload as the HTML views, JSON-encoded."""
    payload = await build_24h_active()
    return JSONResponse(dict(payload))
