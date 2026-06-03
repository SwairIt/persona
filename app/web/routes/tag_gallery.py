"""Per-tag thumbnail gallery — paginated grid of every shot for one tag.

The existing ``/tags/{tag}`` detail page intentionally mixes signals
(activity sparkline, co-tag chart, rename/merge/delete controls) and
only renders the first batch of screenshots underneath. That is great
as a tag landing page, but it is hostile as a way to actually *browse*
every shot that carries the tag — the page also embeds management
forms that scroll off-screen and the thumbnail grid changes column
count depending on viewport.

This module fixes the browsing case:

* ``GET /tags/{tag}/gallery``         — HTML page, 4-column thumbnail
  grid (60 shots/page) with prev/next pager.
* ``GET /api/tags/{tag}/gallery.json`` — same shape as JSON.

The page is read-only by design: any mutation (rename, merge, delete,
re-tag) already exists on the tag detail page and on the bulk-select
toolbar. Keeping this route pure means we never have to wire up a
CSRF token here and the gallery stays linkable / cacheable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.tag_gallery")

router = APIRouter(tags=["tag-gallery"])

# 60 thumbnails per page — fills a 4-column grid 15 rows deep, which
# is dense enough to scan but still loads in under a second over a
# warm SQLite cache even on a tag with tens of thousands of shots.
_PAGE_SIZE = 60
# Hard ceiling on ``?page=`` so a crawler asking for ``page=10**9``
# can't force us to compute pointless OFFSET arithmetic.
_PAGE_MAX = 10_000


def _clamp_page(page: int) -> int:
    """Bound the page index to ``1..PAGE_MAX`` (1-indexed for the UI)."""
    if page < 1:
        return 1
    if page > _PAGE_MAX:
        return _PAGE_MAX
    return page


async def _lookup_tag(conn: aiosqlite.Connection, name: str) -> dict[str, Any] | None:
    """Resolve a tag by its case-insensitive ``name`` plus shot count.

    Returns ``None`` when the tag does not exist so the caller can map
    that into a 404 response. ``name`` is bound through a parameter
    placeholder so a tag like ``%`` cannot smuggle a wildcard into the
    ``=`` comparison.
    """
    cursor = await conn.execute(
        """
        SELECT t.id   AS id,
               t.name AS name,
               t.color AS color,
               (SELECT COUNT(*) FROM screenshot_tags st WHERE st.tag_id = t.id)
                   AS shot_count
        FROM tags t
        WHERE t.name = ?
        """,
        (name.strip().lower(),),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "name": str(row["name"]),
        "color": row["color"],
        "shot_count": int(row["shot_count"] or 0),
    }


async def _fetch_page(
    conn: aiosqlite.Connection,
    tag_id: int,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """Return up to ``limit`` shots for ``tag_id`` starting at ``offset``.

    Ordered newest-first (``captured_at DESC, id DESC``) so the first
    page always shows the most recent activity on the tag. Only the
    columns the thumbnail card actually renders are selected — the
    OCR blob is intentionally excluded.
    """
    cursor = await conn.execute(
        """
        SELECT s.id            AS id,
               s.captured_at   AS captured_at,
               s.thumbnail_path AS thumbnail_path,
               s.app_name      AS app_name,
               s.window_title  AS window_title,
               s.ocr_status    AS ocr_status,
               s.tier          AS tier
        FROM screenshots s
        JOIN screenshot_tags st ON st.screenshot_id = s.id
        WHERE st.tag_id = ?
        ORDER BY s.captured_at DESC, s.id DESC
        LIMIT ? OFFSET ?
        """,
        (tag_id, limit, offset),
    )
    rows = await cursor.fetchall()
    return [dict(row) for row in rows]


@router.get("/tags/{tag}/gallery", response_class=HTMLResponse)
async def tag_gallery_page(
    request: Request,
    tag: str,
    page: int = Query(default=1),
) -> HTMLResponse:
    """Render the paginated 4-column thumbnail grid for ``tag``."""
    safe_page = _clamp_page(page)
    offset = (safe_page - 1) * _PAGE_SIZE

    async with get_connection() as conn:
        tag_row = await _lookup_tag(conn, tag)
        if tag_row is None:
            raise HTTPException(status_code=404, detail=f"Tag not found: {tag}")
        shots = await _fetch_page(conn, tag_row["id"], _PAGE_SIZE, offset)

    total = tag_row["shot_count"]
    # Round up — ``(total + PAGE_SIZE - 1) // PAGE_SIZE`` avoids float math.
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    tag_name = str(tag_row["name"])
    encoded_tag = quote(tag_name, safe="")

    log.info(
        "tag_gallery.render",
        tag=tag_name,
        tag_id=tag_row["id"],
        page=safe_page,
        page_size=_PAGE_SIZE,
        shots_on_page=len(shots),
        total=total,
        total_pages=total_pages,
    )

    return templates.TemplateResponse(
        request,
        "tag_gallery.html",
        {
            "title": f"Gallery · {tag_name}",
            "active_nav": "tags",
            "tag": tag_row,
            "shots": shots,
            "page": safe_page,
            "page_size": _PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "has_prev": safe_page > 1,
            "has_next": safe_page < total_pages,
            "detail_url": f"/tags/{tag_row['id']}",
            "trend_url": f"/tags/{encoded_tag}/trend",
            "json_url": f"/api/tags/{encoded_tag}/gallery.json?page={safe_page}",
            "gallery_base": f"/tags/{encoded_tag}/gallery",
        },
    )


@router.get("/api/tags/{tag}/gallery.json", response_class=JSONResponse)
async def tag_gallery_json(
    tag: str,
    page: int = Query(default=1),
) -> JSONResponse:
    """JSON projection of the same page — keys mirror the template."""
    safe_page = _clamp_page(page)
    offset = (safe_page - 1) * _PAGE_SIZE

    async with get_connection() as conn:
        tag_row = await _lookup_tag(conn, tag)
        if tag_row is None:
            raise HTTPException(status_code=404, detail=f"Tag not found: {tag}")
        shots = await _fetch_page(conn, tag_row["id"], _PAGE_SIZE, offset)

    total = tag_row["shot_count"]
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    log.info(
        "tag_gallery.json",
        tag=tag_row["name"],
        tag_id=tag_row["id"],
        page=safe_page,
        shots_on_page=len(shots),
        total=total,
    )

    return JSONResponse(
        {
            "tag": {
                "id": tag_row["id"],
                "name": tag_row["name"],
                "color": tag_row["color"],
            },
            "page": safe_page,
            "page_size": _PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "has_prev": safe_page > 1,
            "has_next": safe_page < total_pages,
            "shots": shots,
        }
    )
