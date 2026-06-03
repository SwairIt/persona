"""v0.77 — ``/admin/share-analytics`` dashboard + JSON sibling.

Pairs with :mod:`app.share_analytics`. The HTML route renders a
3-column Tailwind layout (top shots, top IP-prefixes, daily SVG line
chart) by handing the precomputed :class:`~app.share_analytics.ShareAnalytics`
payload straight to the template. The JSON route returns the same
payload unwrapped under ``/api/share-analytics.json`` so an external
dashboard or a polling script can consume it without parsing HTML.

The ``days`` query parameter is the only knob: it is forwarded as-is to
:func:`app.share_analytics.compute_share_analytics`, which clamps it
into ``[1, 365]`` and uses parametrised SQL — see the module's
docstring for the rationale.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.share_analytics import compute_share_analytics
from app.web.templates_engine import templates

router = APIRouter(tags=["share-analytics"])
log = get_logger("persona.share_analytics")

# Window bounds mirror :mod:`app.share_analytics`. We re-declare them
# here so FastAPI can enforce the clamp at the request-validation
# layer (returning 422 for out-of-range values) before the aggregator
# silently clamps a wild integer down to the inner ``_MAX_DAYS``.
_DEFAULT_DAYS = 30
_MIN_DAYS = 1
_MAX_DAYS = 365


@router.get("/admin/share-analytics", response_class=HTMLResponse)
async def share_analytics_page(
    request: Request,
    days: int = Query(_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> HTMLResponse:
    """Render the share-analytics dashboard for the last ``days`` days."""
    payload = await compute_share_analytics(days=days)

    total_visits = sum(row["visits"] for row in payload["daily"])
    max_daily = max(
        (row["visits"] for row in payload["daily"]),
        default=0,
    )
    max_top_shot_visits = max(
        (row["visits"] for row in payload["top_shots"]),
        default=0,
    )
    max_top_ip_count = max(
        (row["count"] for row in payload["top_ip_prefixes"]),
        default=0,
    )

    log.debug(
        "share_analytics.page.rendered",
        days=days,
        total_visits=total_visits,
        top_shots=len(payload["top_shots"]),
        top_ip_prefixes=len(payload["top_ip_prefixes"]),
    )

    return templates.TemplateResponse(
        request,
        "share_analytics.html",
        {
            "title": "Share analytics",
            "active_nav": "stats",
            "days_window": days,
            "top_shots": payload["top_shots"],
            "top_ip_prefixes": payload["top_ip_prefixes"],
            "daily": payload["daily"],
            "total_visits": total_visits,
            "max_daily": max_daily,
            "max_top_shot_visits": max_top_shot_visits,
            "max_top_ip_count": max_top_ip_count,
        },
    )


@router.get("/api/share-analytics.json")
async def share_analytics_json(
    days: int = Query(_DEFAULT_DAYS, ge=_MIN_DAYS, le=_MAX_DAYS),
) -> JSONResponse:
    """Return the share-analytics payload as JSON.

    Shape: ``{"days": N, "top_shots": [...], "top_ip_prefixes": [...],
    "daily": [...]}``. ``daily`` is oldest first with gaps zero-filled,
    matching the SVG line chart on the HTML page.
    """
    payload = await compute_share_analytics(days=days)
    return JSONResponse(
        {
            "days": days,
            "top_shots": [dict(row) for row in payload["top_shots"]],
            "top_ip_prefixes": [dict(row) for row in payload["top_ip_prefixes"]],
            "daily": [dict(row) for row in payload["daily"]],
        }
    )


__all__ = ["router"]
