"""Web UI + JSON API for the opt-in clipboard history.

Routes:
    * ``GET  /clipboard``                 — paginated HTML history (50/page)
                                            with a ``q`` LIKE-search box.
    * ``POST /clipboard/{id}/delete``     — drop a single entry. Redirects
                                            back to ``/clipboard?page=…``
                                            so the user keeps their place.
    * ``GET  /api/clipboard.json``        — JSON list (same pagination +
                                            search), for the browser ext.

Search uses parametrised ``LIKE %?%`` against the (already-redacted)
``text`` column — secrets the user copied earlier are masked in the
search corpus too, since redaction happens in the worker before insert.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.clipboard")

router = APIRouter(tags=["clipboard"])

_PAGE_SIZE = 50
_PREVIEW_CHARS = 200


def _escape_like(term: str) -> str:
    """Escape ``%`` / ``_`` / ``\\`` for SQLite LIKE with ESCAPE '\\'."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _count_events(
    conn: aiosqlite.Connection,
    *,
    query: str,
) -> int:
    if query:
        like = f"%{_escape_like(query)}%"
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM clipboard_event "
            "WHERE text LIKE ? ESCAPE '\\'",
            (like,),
        )
    else:
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM clipboard_event")
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["n"])


async def _list_events_page(
    conn: aiosqlite.Connection,
    *,
    query: str,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """One page of clipboard events, newest first, optional LIKE filter."""
    if query:
        like = f"%{_escape_like(query)}%"
        cursor = await conn.execute(
            """
            SELECT id, captured_at, text, length, app_name, hash
            FROM clipboard_event
            WHERE text LIKE ? ESCAPE '\\'
            ORDER BY captured_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (like, limit, offset),
        )
    else:
        cursor = await conn.execute(
            """
            SELECT id, captured_at, text, length, app_name, hash
            FROM clipboard_event
            ORDER BY captured_at DESC, id DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
    rows = await cursor.fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        text = str(row["text"])
        preview = text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…"
        items.append(
            {
                "id": int(row["id"]),
                "captured_at": str(row["captured_at"]),
                "preview": preview,
                "length": int(row["length"]),
                "app_name": (str(row["app_name"]) if row["app_name"] is not None else None),
                "hash": str(row["hash"]),
            }
        )
    return items


@router.get("/clipboard", response_class=HTMLResponse)
async def clipboard_page(
    request: Request,
    page: int = Query(default=1, ge=1, le=10_000),
    q: str = Query(default=""),
) -> HTMLResponse:
    """Render the clipboard history page (50/page, optional search)."""
    query = q.strip()
    offset = (page - 1) * _PAGE_SIZE
    async with get_connection() as conn:
        items = await _list_events_page(
            conn,
            query=query,
            limit=_PAGE_SIZE,
            offset=offset,
        )
        total = await _count_events(conn, query=query)

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    log.info(
        "clipboard.page",
        page=page,
        item_count=len(items),
        total=total,
        has_query=bool(query),
    )
    return templates.TemplateResponse(
        request,
        "clipboard.html",
        {
            "title": "Clipboard",
            "active_nav": "timeline",
            "items": items,
            "query": query,
            "page": page,
            "page_size": _PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "has_prev": page > 1,
            "has_next": page < total_pages,
        },
    )


@router.post("/clipboard/{entry_id}/delete")
async def clipboard_delete(
    entry_id: int,
    page: int = Form(default=1),
    q: str = Form(default=""),
) -> RedirectResponse:
    """Remove a single clipboard entry, then redirect back to the page."""
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM clipboard_event WHERE id = ?",
            (entry_id,),
        )
        await conn.commit()
    log.info("clipboard.deleted", entry_id=entry_id)
    target = f"/clipboard?page={max(1, page)}"
    if q:
        from urllib.parse import quote  # noqa: PLC0415 — local import to avoid global cost

        target += f"&q={quote(q)}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/api/clipboard.json", response_class=JSONResponse)
async def clipboard_json(
    page: int = Query(default=1, ge=1, le=10_000),
    q: str = Query(default=""),
) -> JSONResponse:
    """Paginated JSON view of clipboard events."""
    query = q.strip()
    offset = (page - 1) * _PAGE_SIZE
    async with get_connection() as conn:
        items = await _list_events_page(
            conn,
            query=query,
            limit=_PAGE_SIZE,
            offset=offset,
        )
        total = await _count_events(conn, query=query)

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    log.info(
        "clipboard.json",
        page=page,
        item_count=len(items),
        total=total,
        has_query=bool(query),
    )
    return JSONResponse(
        {
            "page": page,
            "page_size": _PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "query": query,
            "items": items,
        }
    )
