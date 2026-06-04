"""Weekly cards dashboard + JSON API (tier 2, v1.15).

Renders the most recent N weekly_card rows on ``/memory/weeks`` and
exposes the same list as JSON at ``/api/memory/weekly.json``.

Shipped as a separate page so this feature is self-contained — the
coordinator can later merge the section into the main ``/memory`` page
without having to touch this module.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.web.templates_engine import templates
from app.weekly_card import list_weekly_cards

router = APIRouter(tags=["memory"])


@router.get("/memory/weeks", response_class=HTMLResponse)
async def memory_weeks_page(request: Request) -> HTMLResponse:
    """Render the last 12 weekly cards (newest first)."""
    cards = await list_weekly_cards(limit=12)
    return templates.TemplateResponse(
        request,
        "weekly_cards.html",
        {
            "title": "Память — недели",
            "active_nav": "memory",
            "cards": cards,
        },
    )


@router.get("/api/memory/weekly.json")
async def memory_weekly_json(
    limit: int = Query(12, ge=1, le=520),
) -> JSONResponse:
    """JSON sibling — last N weekly cards. Default 12, max ~10 years."""
    cards = await list_weekly_cards(limit=limit)
    return JSONResponse({"count": len(cards), "items": cards})


__all__ = ["router"]
