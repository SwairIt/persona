"""Web UI + JSON API for the clipboard semantic search surface.

Routes:
    * ``GET /clipboard/semantic``        — HTML page (extends base.html)
                                           with a search input + ranked
                                           result list. Each hit shows a
                                           snippet, similarity badge,
                                           relative timestamp and a
                                           "Скопировать" button that
                                           writes the *full* text back
                                           to ``navigator.clipboard``.
    * ``GET /api/clipboard/semantic.json`` — JSON envelope of the same
                                           result, for browser extensions
                                           and headless scripts.

The actual scoring lives in :mod:`app.clipboard_embeddings`. The route
layer is a thin adapter: validate the query, call the semantic search
helper, render a list of dicts. When ``fastembed`` / ``numpy`` are not
installed the helper returns an empty list and the page renders a
graceful "no results" panel — exactly the same shape as a query that
genuinely matched nothing.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.clipboard_embeddings import search_clipboard_semantic
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.clipboard_semantic")

router = APIRouter(tags=["clipboard"])

# Page-size cap — the underlying helper already caps the candidate pool
# at 5000, but keeping the surfaced list small means the HTML stays
# readable on a phone and the JSON envelope stays small enough to
# stream over a slow link without a paginator.
_RESULT_LIMIT: int = 20


@router.get("/api/clipboard/semantic.json", response_class=JSONResponse)
async def clipboard_semantic_json(
    q: str = Query(default=""),
) -> JSONResponse:
    """Semantic clipboard search as JSON.

    Returns ``{"query": str, "results": list[dict]}``. Each result has
    ``id`` / ``content_text`` / ``snippet`` / ``created_at`` /
    ``similarity`` (see :func:`app.clipboard_embeddings.search_clipboard_semantic`
    for the exact contract). An empty query yields an empty list — we
    do not 400 because the HTML page renders the same endpoint on first
    load with no query yet.
    """
    query = q.strip()
    results: list[dict[str, Any]] = []
    if query:
        results = await search_clipboard_semantic(query, limit=_RESULT_LIMIT)
    log.info(
        "clipboard_semantic.json",
        has_query=bool(query),
        result_count=len(results),
    )
    return JSONResponse(
        {
            "query": query,
            "results": results,
        }
    )


@router.get("/clipboard/semantic", response_class=HTMLResponse)
async def clipboard_semantic_page(
    request: Request,
    q: str = Query(default=""),
) -> HTMLResponse:
    """HTML semantic-search page for clipboard history."""
    query = q.strip()
    results: list[dict[str, Any]] = []
    if query:
        results = await search_clipboard_semantic(query, limit=_RESULT_LIMIT)
    log.info(
        "clipboard_semantic.page",
        has_query=bool(query),
        result_count=len(results),
    )
    return templates.TemplateResponse(
        request,
        "clipboard_semantic.html",
        {
            "title": "Поиск буфера",
            "active_nav": "search",
            "query": query,
            "results": results,
            "result_limit": _RESULT_LIMIT,
        },
    )
