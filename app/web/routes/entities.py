"""Entity ledger — people, projects, topics extracted from hourly cards (v1.27).

Three views:

- ``GET /entities`` — three-column dashboard (People / Projects / Topics)
  with mention counts and "last seen" relative timestamps.
- ``GET /api/entities.json`` — same data, JSON, optionally filtered by
  ``kind`` query param.
- ``GET /entity/{id}`` — single-entity timeline; one row per mention
  joined with the source hourly_card summary.

The page is read-only — writes are owned by the
:mod:`app.workers.entity_extractor_worker`.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.entity_extractor import (
    get_entity,
    get_entity_timeline,
    get_top_entities,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["entities"])


_KINDS: tuple[str, ...] = ("person", "project", "topic", "other")
"""Valid CHECK values from migration 110."""


@router.get("/entities", response_class=HTMLResponse)
async def entities_page(request: Request) -> HTMLResponse:
    """Render the three-column entity dashboard."""
    people = await get_top_entities(kind="person", limit=50)
    projects = await get_top_entities(kind="project", limit=50)
    topics = await get_top_entities(kind="topic", limit=50)
    return templates.TemplateResponse(
        request,
        "entities.html",
        {
            "title": "People + projects + topics",
            "active_nav": "memory",
            "people": people,
            "projects": projects,
            "topics": topics,
        },
    )


@router.get("/api/entities.json")
async def entities_json(
    kind: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
) -> JSONResponse:
    """JSON view — top entities, optionally filtered by ``kind``."""
    if kind is not None and kind not in _KINDS:
        raise HTTPException(status_code=400, detail=f"invalid kind: {kind!r}")
    items = await get_top_entities(kind=kind, limit=limit)
    return JSONResponse({"count": len(items), "items": items})


@router.get("/entity/{entity_id}", response_class=HTMLResponse)
async def entity_detail_page(
    request: Request,
    entity_id: int,
) -> HTMLResponse:
    """Render a single entity's mention timeline."""
    entity = await get_entity(entity_id)
    if entity is None:
        raise HTTPException(status_code=404, detail="entity not found")
    timeline = await get_entity_timeline(entity_id, limit=200)
    return templates.TemplateResponse(
        request,
        "entity_detail.html",
        {
            "title": f"{entity['name']} — entity",
            "active_nav": "memory",
            "entity": entity,
            "timeline": timeline,
        },
    )


__all__ = ["router"]
