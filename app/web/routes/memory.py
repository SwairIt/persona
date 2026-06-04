"""Memory dashboard — view hierarchical memory tiers (v1.15).

The route renders three sections:

- **Recent hours** — last 24 hourly_card rows (newest first), each
  showing apps, screen count, voice minutes, top words, summary.
- **Days** — last 30 daily_pin rows, the tiny 200-byte snapshots that
  survive all retention sweeps.
- **All-time pins** — paginated view of every daily_pin for archive
  browsing.

This page is the user-visible proof that the iterated memory pipeline
actually produces something useful — without it, hourly_cards and
daily_pins live invisibly in the DB.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.daily_pin import list_pins
from app.hourly_card import list_recent_cards
from app.web.templates_engine import templates

router = APIRouter(tags=["memory"])


@router.get("/memory", response_class=HTMLResponse)
async def memory_page(request: Request) -> HTMLResponse:
    """Render the hierarchical memory dashboard."""
    cards = await list_recent_cards(limit=24)
    pins = await list_pins(limit=30)
    return templates.TemplateResponse(
        request,
        "memory.html",
        {
            "title": "Memory",
            "active_nav": "memory",
            "cards": cards,
            "pins": pins,
        },
    )


@router.get("/api/memory/cards.json")
async def memory_cards_json(
    limit: int = Query(24, ge=1, le=168),
) -> JSONResponse:
    """JSON sibling — last N hourly cards for external consumers."""
    cards = await list_recent_cards(limit=limit)
    return JSONResponse({"count": len(cards), "items": cards})


@router.get("/api/memory/pins.json")
async def memory_pins_json(
    limit: int = Query(30, ge=1, le=3650),
) -> JSONResponse:
    """JSON sibling — last N daily pins. Default 30 days, max 10 years."""
    pins = await list_pins(limit=limit)
    return JSONResponse({"count": len(pins), "items": pins})


__all__ = ["router"]
