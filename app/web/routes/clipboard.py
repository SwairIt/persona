"""Web UI + JSON API for the opt-in clipboard history.

Routes:
    * ``GET  /clipboard``                 — paginated HTML history (50/page)
                                            with a ``q`` LIKE-search box and
                                            optional date/length facets.
    * ``POST /clipboard/{id}/delete``     — drop a single entry. Redirects
                                            back to ``/clipboard?page=…``
                                            so the user keeps their place.
    * ``GET  /api/clipboard.json``        — JSON list (same pagination +
                                            search + facets), for the
                                            browser extension.

Search uses parametrised ``LIKE %?%`` against the (already-redacted)
``text`` column — secrets the user copied earlier are masked in the
search corpus too, since redaction happens in the worker before insert.

v0.60 adds *clipboard search facets*: ``date_from`` / ``date_to`` filter
on the stored ``captured_at`` timestamp (``YYYY-MM-DD`` boundaries,
inclusive on both ends), and ``min_length`` / ``max_length`` clip on the
pre-redaction character count. All four are optional and compose with
the existing ``q`` substring search; SQL is parametrised end-to-end.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.clipboard")
facets_log = get_logger("persona.clipboard.facets")

router = APIRouter(tags=["clipboard"])

_PAGE_SIZE = 50
_PREVIEW_CHARS = 200


def _escape_like(term: str) -> str:
    """Escape ``%`` / ``_`` / ``\\`` for SQLite LIKE with ESCAPE '\\'."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalise_date(value: str | None) -> str | None:
    """Accept ``YYYY-MM-DD`` (or longer ISO) and return the date part.

    Anything that doesn't parse cleanly is dropped silently so URL-driven
    traffic with stale or garbled values still works — the HTML form uses
    ``<input type="date">`` which already constrains the user to ISO.
    """
    if not value:
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        return datetime.fromisoformat(candidate).date().isoformat()
    except ValueError:
        return None


def _clamp_length(value: int | None) -> int | None:
    """Drop non-positive length bounds — they would match every row anyway."""
    if value is None:
        return None
    if value < 0:
        return None
    return value


def _build_filters(
    *,
    query: str,
    date_from: str | None,
    date_to: str | None,
    min_length: int | None,
    max_length: int | None,
) -> tuple[str, list[Any]]:
    """Compose the WHERE clause + bind params for the active facets.

    All clauses are AND-ed; the empty-filter case returns ``("", [])``.
    Date bounds are inclusive: ``date_from`` matches the start of that
    day (``YYYY-MM-DD 00:00:00``), ``date_to`` matches up to the start
    of the *next* day so the user's chosen end-day is fully included.
    """
    clauses: list[str] = []
    params: list[Any] = []

    if query:
        clauses.append("text LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(query)}%")
    if date_from:
        clauses.append("captured_at >= ?")
        params.append(f"{date_from} 00:00:00")
    if date_to:
        # ``captured_at < next_day`` keeps the bound inclusive of date_to
        # without needing to know the row's exact seconds/micros.
        clauses.append("captured_at < datetime(?, '+1 day')")
        params.append(date_to)
    if min_length is not None:
        clauses.append("length >= ?")
        params.append(min_length)
    if max_length is not None:
        clauses.append("length <= ?")
        params.append(max_length)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


async def _count_events(
    conn: aiosqlite.Connection,
    *,
    query: str,
    date_from: str | None,
    date_to: str | None,
    min_length: int | None,
    max_length: int | None,
) -> int:
    where, params = _build_filters(
        query=query,
        date_from=date_from,
        date_to=date_to,
        min_length=min_length,
        max_length=max_length,
    )
    # ``where`` is a join of static literals from ``_build_filters``; user
    # input only ever lands in ``params``. The S608 noqa lives on the f-string
    # itself because ruff attributes the warning to that physical line.
    sql_count = f"SELECT COUNT(*) AS n FROM clipboard_event{where}"  # noqa: S608
    cursor = await conn.execute(sql_count, params)
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["n"])


async def _list_events_page(
    conn: aiosqlite.Connection,
    *,
    query: str,
    date_from: str | None,
    date_to: str | None,
    min_length: int | None,
    max_length: int | None,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """One page of clipboard events, newest first, with optional facets."""
    where, params = _build_filters(
        query=query,
        date_from=date_from,
        date_to=date_to,
        min_length=min_length,
        max_length=max_length,
    )
    # See ``_count_events`` for the SQL-safety argument: ``where`` is static
    # literals only; ``params`` carries all user input as bound parameters.
    sql_list = (
        f"SELECT id, captured_at, text, length, app_name, hash FROM clipboard_event{where} "  # noqa: S608
        "ORDER BY captured_at DESC, id DESC LIMIT ? OFFSET ?"
    )
    cursor = await conn.execute(sql_list, [*params, limit, offset])
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


def _facets_active(
    *,
    date_from: str | None,
    date_to: str | None,
    min_length: int | None,
    max_length: int | None,
) -> bool:
    """Anything beyond plain ``q`` is a 'facet' for logging/UI purposes."""
    return bool(date_from or date_to or min_length is not None or max_length is not None)


@router.get("/clipboard", response_class=HTMLResponse)
async def clipboard_page(
    request: Request,
    page: int = Query(default=1, ge=1, le=10_000),
    q: str = Query(default=""),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    min_length: int | None = Query(default=None, ge=0),
    max_length: int | None = Query(default=None, ge=0),
) -> HTMLResponse:
    """Render the clipboard history page (50/page, optional search + facets)."""
    query = q.strip()
    date_from_norm = _normalise_date(date_from)
    date_to_norm = _normalise_date(date_to)
    min_length_norm = _clamp_length(min_length)
    max_length_norm = _clamp_length(max_length)
    offset = (page - 1) * _PAGE_SIZE

    async with get_connection() as conn:
        items = await _list_events_page(
            conn,
            query=query,
            date_from=date_from_norm,
            date_to=date_to_norm,
            min_length=min_length_norm,
            max_length=max_length_norm,
            limit=_PAGE_SIZE,
            offset=offset,
        )
        total = await _count_events(
            conn,
            query=query,
            date_from=date_from_norm,
            date_to=date_to_norm,
            min_length=min_length_norm,
            max_length=max_length_norm,
        )

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    facets_on = _facets_active(
        date_from=date_from_norm,
        date_to=date_to_norm,
        min_length=min_length_norm,
        max_length=max_length_norm,
    )
    log.info(
        "clipboard.page",
        page=page,
        item_count=len(items),
        total=total,
        has_query=bool(query),
        has_facets=facets_on,
    )
    if facets_on:
        facets_log.info(
            "clipboard.facets.applied",
            date_from=date_from_norm,
            date_to=date_to_norm,
            min_length=min_length_norm,
            max_length=max_length_norm,
            total=total,
        )
    return templates.TemplateResponse(
        request,
        "clipboard.html",
        {
            "title": "Clipboard",
            "active_nav": "timeline",
            "items": items,
            "query": query,
            "date_from": date_from_norm or "",
            "date_to": date_to_norm or "",
            "min_length": min_length_norm if min_length_norm is not None else "",
            "max_length": max_length_norm if max_length_norm is not None else "",
            "facets_active": facets_on,
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
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    min_length: int | None = Query(default=None, ge=0),
    max_length: int | None = Query(default=None, ge=0),
) -> JSONResponse:
    """Paginated JSON view of clipboard events (same facets as the HTML page)."""
    query = q.strip()
    date_from_norm = _normalise_date(date_from)
    date_to_norm = _normalise_date(date_to)
    min_length_norm = _clamp_length(min_length)
    max_length_norm = _clamp_length(max_length)
    offset = (page - 1) * _PAGE_SIZE

    async with get_connection() as conn:
        items = await _list_events_page(
            conn,
            query=query,
            date_from=date_from_norm,
            date_to=date_to_norm,
            min_length=min_length_norm,
            max_length=max_length_norm,
            limit=_PAGE_SIZE,
            offset=offset,
        )
        total = await _count_events(
            conn,
            query=query,
            date_from=date_from_norm,
            date_to=date_to_norm,
            min_length=min_length_norm,
            max_length=max_length_norm,
        )

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    facets_on = _facets_active(
        date_from=date_from_norm,
        date_to=date_to_norm,
        min_length=min_length_norm,
        max_length=max_length_norm,
    )
    log.info(
        "clipboard.json",
        page=page,
        item_count=len(items),
        total=total,
        has_query=bool(query),
        has_facets=facets_on,
    )
    if facets_on:
        facets_log.info(
            "clipboard.facets.applied",
            channel="json",
            date_from=date_from_norm,
            date_to=date_to_norm,
            min_length=min_length_norm,
            max_length=max_length_norm,
            total=total,
        )
    return JSONResponse(
        {
            "page": page,
            "page_size": _PAGE_SIZE,
            "total": total,
            "total_pages": total_pages,
            "query": query,
            "filters": {
                "date_from": date_from_norm,
                "date_to": date_to_norm,
                "min_length": min_length_norm,
                "max_length": max_length_norm,
            },
            "items": items,
        }
    )
