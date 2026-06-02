"""User-starred screenshots ("favourites") — discovery shortcut.

Distinct from pin (tier = "pinned"): pin protects a row from auto-demotion
by the tier sweep, while a favourite is purely a quick-access bookmark.
A screenshot may be one, both, or neither.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_screenshot
from app.web.templates_engine import templates

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.favourites")

router = APIRouter(tags=["favourites"])

_PAGE_SIZE = 50


async def _is_favourited(conn: aiosqlite.Connection, screenshot_id: int) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM favourite WHERE screenshot_id = ?",
        (screenshot_id,),
    )
    row = await cursor.fetchone()
    return row is not None


async def _count_favourites(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute("SELECT COUNT(*) AS n FROM favourite")
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["n"])


async def _list_favourites_page(
    conn: aiosqlite.Connection,
    *,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """Return one page of favourites joined to their screenshot row.

    Ordered by starred_at DESC (most recently starred first). Parametrised
    LIMIT/OFFSET — no string interpolation on user input.
    """
    cursor = await conn.execute(
        """
        SELECT
            s.id            AS id,
            s.captured_at   AS captured_at,
            s.app_name      AS app_name,
            s.window_title  AS window_title,
            s.thumbnail_path AS thumbnail_path,
            s.tier          AS tier,
            f.starred_at    AS starred_at
        FROM favourite AS f
        JOIN screenshots AS s ON s.id = f.screenshot_id
        ORDER BY f.starred_at DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "captured_at": str(row["captured_at"]),
            "app_name": (str(row["app_name"]) if row["app_name"] is not None else None),
            "window_title": (str(row["window_title"]) if row["window_title"] is not None else None),
            "thumbnail_path": (
                str(row["thumbnail_path"]) if row["thumbnail_path"] is not None else None
            ),
            "tier": str(row["tier"]) if row["tier"] is not None else "hot",
            "starred_at": str(row["starred_at"]),
        }
        for row in rows
    ]


@router.post(
    "/api/screenshot/{screenshot_id}/favourite",
    response_class=JSONResponse,
)
async def toggle_favourite(screenshot_id: int) -> JSONResponse:
    """Toggle the favourite flag for a screenshot. Returns the new state."""
    async with get_connection() as conn:
        shot = await get_screenshot(conn, screenshot_id)
        if shot is None:
            raise HTTPException(status_code=404, detail="Screenshot not found")

        if await _is_favourited(conn, screenshot_id):
            await conn.execute(
                "DELETE FROM favourite WHERE screenshot_id = ?",
                (screenshot_id,),
            )
            await conn.commit()
            log.info("favourites.unstarred", screenshot_id=screenshot_id)
            return JSONResponse({"id": screenshot_id, "favourited": False})

        await conn.execute(
            "INSERT INTO favourite (screenshot_id) VALUES (?)",
            (screenshot_id,),
        )
        await conn.commit()
    log.info("favourites.starred", screenshot_id=screenshot_id)
    return JSONResponse({"id": screenshot_id, "favourited": True})


@router.get("/favourites", response_class=HTMLResponse)
async def favourites_page(
    request: Request,
    page: int = Query(default=1, ge=1, le=10_000),
) -> HTMLResponse:
    """Render the favourites page — 50 most recently starred shots per page."""
    offset = (page - 1) * _PAGE_SIZE
    async with get_connection() as conn:
        items = await _list_favourites_page(
            conn,
            limit=_PAGE_SIZE,
            offset=offset,
        )
        total = await _count_favourites(conn)

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    log.info(
        "favourites.page",
        page=page,
        item_count=len(items),
        total=total,
    )
    return templates.TemplateResponse(
        request,
        "favourites.html",
        {
            "title": "Favourites",
            "active_nav": "timeline",
            "items": items,
            "page": page,
            "page_size": _PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
    )


@router.get("/api/favourites.json", response_class=JSONResponse)
async def favourites_json(
    page: int = Query(default=1, ge=1, le=10_000),
) -> JSONResponse:
    """Paginated JSON view of favourites (50/page)."""
    offset = (page - 1) * _PAGE_SIZE
    async with get_connection() as conn:
        items = await _list_favourites_page(
            conn,
            limit=_PAGE_SIZE,
            offset=offset,
        )
        total = await _count_favourites(conn)

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    log.info(
        "favourites.json",
        page=page,
        item_count=len(items),
        total=total,
    )
    return JSONResponse(
        {
            "page": page,
            "page_size": _PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "items": items,
        }
    )
