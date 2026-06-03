"""HTTP routes for shareable saved-query collections.

The storage layer lives in :mod:`app.query_collections`; this module is
deliberately thin and only:

* validates form payloads via the storage helpers,
* maps :class:`~app.query_collections.QueryCollectionError` onto HTTP 400,
* renders the public read-only collection page, re-running every member
  query so the visitor sees a live result count.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.logging_setup import get_logger
from app.query_collections import (
    QueryCollectionError,
    add_query,
    create,
    delete,
    get,
    list_all,
)
from app.search import search as run_search
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.query_collections")

router = APIRouter(tags=["query-collections"])

# Re-run cap when computing live counts on the public page. Anything
# above this is reported as ``"500+"`` in the UI; we never want a single
# noisy bookmark to lock up rendering with a multi-second FTS scan.
_COUNT_LIMIT = 500


async def _list_saved_searches() -> list[dict[str, Any]]:
    """Fetch all saved search bookmarks so the "add query" form can offer them.

    Lives here (not in :mod:`app.query_collections`) because it is a
    convenience for the HTML form, not part of the collection storage
    contract.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT slug, title, query "
            "FROM saved_search ORDER BY title COLLATE NOCASE",
        )
        rows = await cursor.fetchall()
    return [
        {
            "slug": str(row["slug"]),
            "title": str(row["title"]),
            "query": str(row["query"]),
        }
        for row in rows
    ]


async def _count_hits(query: str) -> tuple[int, bool]:
    """Return ``(count, capped)`` for a saved-search query.

    Re-runs FTS up to ``_COUNT_LIMIT`` hits. ``capped`` is ``True`` when
    we hit the cap and the real count is unknown; the template renders
    that as ``"500+"`` so visitors aren't misled into thinking the query
    only matches exactly the cap.
    """
    async with get_connection() as conn:
        hits = await run_search(conn, query=query, limit=_COUNT_LIMIT)
    return len(hits), len(hits) >= _COUNT_LIMIT


@router.get("/collections/queries", response_class=HTMLResponse)
async def list_query_collections_page(request: Request) -> HTMLResponse:
    """Admin-style index of every collection plus the create form."""
    collections = await list_all()
    saved_searches = await _list_saved_searches()
    return templates.TemplateResponse(
        request,
        "query_collections.html",
        {
            "title": "Query collections",
            "active_nav": "search",
            "collections": collections,
            "saved_searches": saved_searches,
        },
    )


@router.get("/collections/queries/{slug}", response_class=HTMLResponse)
async def view_query_collection(request: Request, slug: str) -> HTMLResponse:
    """Public read-only page: every member query plus its live result count."""
    try:
        collection = await get(slug)
    except QueryCollectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if collection is None:
        raise HTTPException(status_code=404, detail="Collection not found")

    members_view: list[dict[str, Any]] = []
    for member in collection["members"]:
        count, capped = await _count_hits(member["query"])
        members_view.append(
            {
                "slug": member["slug"],
                "title": member["title"],
                "query": member["query"],
                "position": member["position"],
                "result_count": count,
                "result_capped": capped,
                "search_url": f"/search?q={quote(member['query'], safe='')}",
            },
        )

    return templates.TemplateResponse(
        request,
        "query_collection_public.html",
        {
            "title": collection["title"],
            "active_nav": "search",
            "collection": collection,
            "members": members_view,
        },
    )


@router.post("/collections/queries")
async def create_query_collection(
    slug: str = Form(...),
    title: str = Form(...),
    blurb: str | None = Form(None),
) -> RedirectResponse:
    """Create a new collection."""
    try:
        await create(slug=slug, title=title, blurb=blurb)
    except QueryCollectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/collections/queries", status_code=303)


@router.post("/collections/queries/{slug}/add-query")
async def add_query_to_collection(
    slug: str,
    search_slug: str = Form(...),
    position: int = Form(0),
) -> RedirectResponse:
    """Attach a saved search to a collection."""
    try:
        await add_query(
            collection_slug=slug,
            search_slug=search_slug,
            position=position,
        )
    except QueryCollectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(
        url=f"/collections/queries/{slug}",
        status_code=303,
    )


@router.post("/collections/queries/{slug}/delete")
async def delete_query_collection(slug: str) -> RedirectResponse:
    """Remove a collection and all its memberships."""
    try:
        await delete(slug)
    except QueryCollectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(url="/collections/queries", status_code=303)
