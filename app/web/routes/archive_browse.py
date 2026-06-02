"""Browse + search routes over the satellite archive DB."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.logging_setup import get_logger
from app.storage.archive_browse import archive_recent, archive_search, archive_total
from app.web.templates_engine import templates

router = APIRouter(tags=["archive-browse"])

log = get_logger("persona.archive.browse")


@router.get("/archive/search", response_class=HTMLResponse)
async def archive_search_page(
    request: Request,
    q: str = Query(default=""),
) -> HTMLResponse:
    """Render FTS5 search results from the archive DB."""
    hits = await archive_search(q, limit=50) if q else []
    total = await archive_total()
    log.info(
        "archive.browse.search",
        q_length=len(q),
        hit_count=len(hits),
        archive_total=total,
    )
    return templates.TemplateResponse(
        request,
        "archive_search.html",
        {
            "title": f"Archive search: {q}" if q else "Archive search",
            "active_nav": "archive",
            "query": q,
            "hits": hits,
            "total": total,
        },
    )


@router.get("/archive/browse", response_class=HTMLResponse)
async def archive_recent_page(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
) -> HTMLResponse:
    """Render the most recently archived rows (no FTS)."""
    rows = await archive_recent(limit=limit)
    total = await archive_total()
    log.info(
        "archive.browse.recent",
        limit=limit,
        row_count=len(rows),
        archive_total=total,
    )
    return templates.TemplateResponse(
        request,
        "archive_recent.html",
        {
            "title": "Archive — recent",
            "active_nav": "archive",
            "rows": rows,
            "total": total,
            "limit": limit,
        },
    )
