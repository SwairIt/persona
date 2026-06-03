"""HTML route for the corpus-wide search page.

Endpoint
--------
* ``GET /search/everything?q=TERM`` — render a single Tailwind page
  with five tabs (shots / notes / annotations / stickies / clipboard)
  fed by :func:`app.corpus_search.corpus_search`.

The page is the operator-facing complement of the per-source pages
(``/search``, ``/notes/search``, ``/stickers/search``,
``/clipboard``): one query box, five buckets in parallel, ranked or
recency-sorted per source. Each bucket is capped at 50 rows so a
single noisy source cannot starve the others.

The route is read-only, always returns HTTP 200 (empty / no-match
inputs render the empty-state panel rather than 4xx-ing), and never
splices the user query into SQL — all sanitisation lives in
:mod:`app.corpus_search`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.corpus_search import KINDS, corpus_search
from app.logging_setup import get_logger
from app.web.templates_engine import templates

log = get_logger("persona.corpus_search.routes")

router = APIRouter(tags=["corpus-search"])

# Mirror of :data:`app.corpus_search._MAX_QUERY_LEN` — duplicated here
# (small constant, no behavioural drift risk) so the truncation happens
# at the request boundary too and the template never sees an
# unreasonable string in its ``{{ query }}`` echo.
_MAX_QUERY_LEN = 200

# Per-source row cap surfaced into the template so the "capped at N"
# hint stays in sync with the underlying helper call.
_LIMIT = 50


@router.get("/search/everything", response_class=HTMLResponse)
async def corpus_search_page(
    request: Request,
    q: str = Query(default=""),
) -> HTMLResponse:
    """Render the combined search page. Always 200, even on empty input."""
    query = (q or "").strip()[:_MAX_QUERY_LEN]
    results: dict[str, list[dict[str, Any]]] = {kind: [] for kind in KINDS}
    total = 0
    if query:
        results = await corpus_search(query, limit=_LIMIT)
        total = sum(len(results[kind]) for kind in KINDS)
        log.info(
            "corpus_search.page",
            query=query,
            total=total,
            shots=len(results["shots"]),
            notes=len(results["notes"]),
            annotations=len(results["annotations"]),
            stickies=len(results["stickies"]),
            clipboard=len(results["clipboard"]),
        )

    counts: dict[str, int] = {kind: len(results[kind]) for kind in KINDS}

    return templates.TemplateResponse(
        request,
        "corpus_search.html",
        {
            "title": f"Everything: {query}" if query else "Search everything",
            "active_nav": "search",
            "query": query,
            "results": results,
            "counts": counts,
            "total": total,
            "limit": _LIMIT,
        },
    )
