"""Saved search bookmarks — explicit user-pinned queries.

Separate from the auto-tracked ``search_history`` table (migration 016):
bookmarks have a human-readable title and live until the user removes them.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import quote

import aiosqlite
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

log = get_logger("persona.saved_searches")

router = APIRouter(tags=["saved-searches"])

SLUG_RE = re.compile(r"^[a-z0-9-]{1,40}$")
TITLE_MIN, TITLE_MAX = 1, 100
QUERY_MIN, QUERY_MAX = 1, 500


def _validate_slug(slug: str) -> str:
    cleaned = (slug or "").strip().lower()
    if not SLUG_RE.match(cleaned):
        msg = "slug must match ^[a-z0-9-]{1,40}$"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned


def _validate_title(title: str) -> str:
    cleaned = (title or "").strip()
    if not (TITLE_MIN <= len(cleaned) <= TITLE_MAX):
        msg = f"title must be {TITLE_MIN}..{TITLE_MAX} characters"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned


def _validate_query(query: str) -> str:
    cleaned = (query or "").strip()
    if not (QUERY_MIN <= len(cleaned) <= QUERY_MAX):
        msg = f"query must be {QUERY_MIN}..{QUERY_MAX} characters"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned


async def _list_all(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT slug, title, query, created_at "
        "FROM saved_search ORDER BY created_at DESC",
    )
    rows = await cursor.fetchall()
    return [
        {
            "slug": str(row["slug"]),
            "title": str(row["title"]),
            "query": str(row["query"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def _get_one(
    conn: aiosqlite.Connection,
    slug: str,
) -> dict[str, Any] | None:
    cursor = await conn.execute(
        "SELECT slug, title, query, created_at FROM saved_search WHERE slug = ?",
        (slug,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "slug": str(row["slug"]),
        "title": str(row["title"]),
        "query": str(row["query"]),
        "created_at": str(row["created_at"]),
    }


@router.get("/searches", response_class=HTMLResponse)
async def saved_searches_page(request: Request) -> HTMLResponse:
    """Render the bookmarks page."""
    async with get_connection() as conn:
        items = await _list_all(conn)
    return templates.TemplateResponse(
        request,
        "saved_searches.html",
        {
            "title": "Saved searches",
            "active_nav": "search",
            "items": items,
        },
    )


@router.post("/searches")
async def saved_searches_create(
    slug: str = Form(...),
    title: str = Form(...),
    query: str = Form(...),
) -> RedirectResponse:
    """Add a new bookmark."""
    slug_v = _validate_slug(slug)
    title_v = _validate_title(title)
    query_v = _validate_query(query)

    async with get_connection() as conn:
        try:
            await conn.execute(
                "INSERT INTO saved_search (slug, title, query) VALUES (?, ?, ?)",
                (slug_v, title_v, query_v),
            )
            await conn.commit()
        except aiosqlite.IntegrityError as exc:
            log.warning("saved_searches.duplicate", slug=slug_v)
            msg = f"slug {slug_v!r} already exists"
            raise HTTPException(status_code=400, detail=msg) from exc

    log.info("saved_searches.created", slug=slug_v, title=title_v)
    return RedirectResponse(url="/searches", status_code=303)


@router.post("/searches/{slug}/delete")
async def saved_searches_delete(slug: str) -> RedirectResponse:
    """Remove a bookmark by slug (no-op if missing)."""
    slug_v = _validate_slug(slug)
    async with get_connection() as conn:
        await conn.execute("DELETE FROM saved_search WHERE slug = ?", (slug_v,))
        await conn.commit()
    log.info("saved_searches.deleted", slug=slug_v)
    return RedirectResponse(url="/searches", status_code=303)


@router.get("/searches/{slug}")
async def saved_searches_run(slug: str) -> RedirectResponse:
    """Redirect to /search?q=<bookmarked query>."""
    slug_v = _validate_slug(slug)
    async with get_connection() as conn:
        bookmark = await _get_one(conn, slug_v)
    if bookmark is None:
        raise HTTPException(status_code=404, detail="Saved search not found")
    encoded = quote(bookmark["query"], safe="")
    log.info("saved_searches.run", slug=slug_v)
    return RedirectResponse(url=f"/search?q={encoded}", status_code=303)
