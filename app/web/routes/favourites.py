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
log_sort = get_logger("persona.grid_sort")

router = APIRouter(tags=["favourites"])

_PAGE_SIZE = 50

# Map whitelisted ``sort_by`` query values to the literal ORDER BY clause
# they expand into. Keys are validated against this dict before any SQL
# is built — the resulting clause is a constant string, so no user input
# is ever interpolated into the query.
_SORT_CLAUSES: dict[str, str] = {
    # Default keeps historical behaviour: most recently *starred* first.
    "captured_at": "f.starred_at DESC",
    "captured_at_asc": "s.captured_at ASC",
    "app_name": "(s.app_name IS NULL), s.app_name ASC, s.captured_at DESC",
    "ocr_length": "COALESCE(LENGTH(s.ocr_text), 0) DESC, s.captured_at DESC",
}
_DEFAULT_SORT = "captured_at"


def _coerce_sort(value: str | None) -> str:
    """Reduce arbitrary user input to a whitelisted sort key."""
    if value and value in _SORT_CLAUSES:
        return value
    return _DEFAULT_SORT


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
    sort_by: str = _DEFAULT_SORT,
) -> list[dict[str, Any]]:
    """Return one page of favourites joined to their screenshot row.

    The default order is ``starred_at DESC`` (most recently starred
    first). When ``sort_by`` is supplied the clause is taken verbatim
    from the :data:`_SORT_CLAUSES` whitelist — unknown values fall back
    silently. Parametrised LIMIT/OFFSET — no string interpolation on
    user input ever touches SQL.
    """
    order_clause = _SORT_CLAUSES.get(sort_by, _SORT_CLAUSES[_DEFAULT_SORT])

    # The ``order_clause`` value is a constant string from the
    # whitelist above — never user-supplied text — so the literal-
    # concatenation here is safe by construction.
    cursor = await conn.execute(
        f"""
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
        ORDER BY {order_clause}
        LIMIT ? OFFSET ?
        """,  # noqa: S608
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
    sort_by: str = Query(default=_DEFAULT_SORT),
) -> HTMLResponse:
    """Render the favourites page — 50 most recently starred shots per page."""
    offset = (page - 1) * _PAGE_SIZE
    sort_key = _coerce_sort(sort_by)
    async with get_connection() as conn:
        items = await _list_favourites_page(
            conn,
            limit=_PAGE_SIZE,
            offset=offset,
            sort_by=sort_key,
        )
        total = await _count_favourites(conn)

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    log.info(
        "favourites.page",
        page=page,
        item_count=len(items),
        total=total,
    )
    if sort_key != _DEFAULT_SORT:
        log_sort.info("grid_sort.favourites", sort_by=sort_key, count=len(items))
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
            "sort_by": sort_key,
            "sort_options": list(_SORT_CLAUSES.keys()),
        },
    )


@router.get("/api/favourites.json", response_class=JSONResponse)
async def favourites_json(
    page: int = Query(default=1, ge=1, le=10_000),
    sort_by: str = Query(default=_DEFAULT_SORT),
) -> JSONResponse:
    """Paginated JSON view of favourites (50/page)."""
    offset = (page - 1) * _PAGE_SIZE
    sort_key = _coerce_sort(sort_by)
    async with get_connection() as conn:
        items = await _list_favourites_page(
            conn,
            limit=_PAGE_SIZE,
            offset=offset,
            sort_by=sort_key,
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
            "sort_by": sort_key,
            "items": items,
        }
    )
