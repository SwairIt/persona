"""User-defined dashboard widgets — saved FTS queries rendered as tiles.

v0.86 ships the third leg of the "make /dashboard yours" trio:

* v0.65 — fixed five-tile layout.
* v0.81 — :mod:`app.web.routes.dashboard_tiles` lets the user reorder
  / hide the built-in tiles.
* v0.86 — *this* module lets the user *add* their own tiles. A widget
  is a labelled saved search; the tile renders the title and the live
  count of matches for the query.

Storage shape lives in migration ``075_dashboard_widgets.sql``: a
single ``dashboard_widget`` table keyed by autoincrement id with
``title``, ``query``, ``position``, ``created_at``. We deliberately
re-use :func:`app.search.search` rather than reaching into FTS5
directly — that keeps query sanitisation (``_sanitise_query``) and the
filter contract in one place, and the count is just ``len(hits)`` of a
small bounded limit (the dashboard never needs to know "how many
beyond N" — a saturating display is fine and protects us from a
runaway query exploding the request).

The render path is consumed by :mod:`app.web.routes.dashboard` via
:func:`collect_widgets`: it runs every widget's query against the live
DB and returns ``{id, title, query, count, saturated}`` rows for the
template to iterate after the built-in tiles.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.logging_setup import get_logger
from app.search import search as run_search
from app.storage.db import get_connection
from app.web.templates_engine import templates

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.dashboard_widgets")

router = APIRouter(tags=["dashboard"])

# Bounds picked to match the surrounding "single-line user-typed
# string" pattern (saved_search title/query) — wide enough for any
# realistic FTS5 expression, narrow enough that an accidental paste of
# a megabyte of OCR text can't be persisted.
TITLE_MIN, TITLE_MAX = 1, 100
QUERY_MIN, QUERY_MAX = 1, 500

# The dashboard tile only needs a "lots vs few" hint, not an exact
# total. Capping the search at this many hits means a pathological
# widget can't dominate the dashboard render budget — anything at or
# above the cap is shown as ``COUNT_CAP+`` in the template.
COUNT_CAP = 200


def _validate_title(title: str) -> str:
    """Trim + bounds-check a widget title; raise 400 on bad input."""
    cleaned = (title or "").strip()
    if not (TITLE_MIN <= len(cleaned) <= TITLE_MAX):
        msg = f"title must be {TITLE_MIN}..{TITLE_MAX} characters"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned


def _validate_query(query: str) -> str:
    """Trim + bounds-check a widget query; raise 400 on bad input."""
    cleaned = (query or "").strip()
    if not (QUERY_MIN <= len(cleaned) <= QUERY_MAX):
        msg = f"query must be {QUERY_MIN}..{QUERY_MAX} characters"
        raise HTTPException(status_code=400, detail=msg)
    return cleaned


async def _list_widgets(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Return all widgets ordered by ``position`` then ``id``.

    The secondary ``id`` ordering keeps two widgets inserted with the
    same ``position`` (defensive — the route layer always picks
    ``MAX(position)+1``, but the column has no UNIQUE constraint)
    deterministic across page loads.
    """
    cursor = await conn.execute(
        "SELECT id, title, query, position, created_at "
        "FROM dashboard_widget ORDER BY position ASC, id ASC",
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "title": str(row["title"]),
            "query": str(row["query"]),
            "position": int(row["position"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def _next_position(conn: aiosqlite.Connection) -> int:
    """Return ``MAX(position) + 1`` (0 for an empty table).

    Using ``MAX+1`` rather than ``COUNT`` means deleting a middle
    widget never causes the next insert to collide with an existing
    row's position. The ordering displayed on /dashboard then becomes
    "insertion order with deletes leaving gaps", which is the simplest
    intuition for a list with no explicit reorder UI in v0.86.
    """
    cursor = await conn.execute(
        "SELECT COALESCE(MAX(position), -1) AS max_pos FROM dashboard_widget",
    )
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["max_pos"]) + 1


async def collect_widgets() -> list[dict[str, Any]]:
    """Render-side helper: list widgets with their live result counts.

    Called from :mod:`app.web.routes.dashboard` after the built-in
    tiles are computed. Each row is augmented with:

    * ``count``      — number of hits returned by the saved query,
                       capped at :data:`COUNT_CAP`.
    * ``saturated``  — ``True`` when the count hit the cap (template
                       renders ``200+`` instead of an exact total).

    A widget whose query raises is logged and rendered with
    ``count=0`` so a single broken widget can never 500 the whole
    dashboard.
    """
    async with get_connection() as conn:
        widgets = await _list_widgets(conn)
        results: list[dict[str, Any]] = []
        for widget in widgets:
            try:
                hits = await run_search(
                    conn,
                    query=widget["query"],
                    limit=COUNT_CAP,
                )
                count = len(hits)
            except Exception as exc:
                # A bad MATCH expression or transient FTS error must not
                # take down the whole dashboard. Log + zero so the tile
                # still renders with a visible "0" rather than a 500.
                log.warning(
                    "dashboard_widgets.query_failed",
                    widget_id=widget["id"],
                    error=str(exc),
                )
                count = 0
            results.append(
                {
                    "id": widget["id"],
                    "title": widget["title"],
                    "query": widget["query"],
                    "count": count,
                    "saturated": count >= COUNT_CAP,
                }
            )
    log.info("dashboard_widgets.collected", count=len(results))
    return results


@router.get("/settings/dashboard-widgets", response_class=HTMLResponse)
async def dashboard_widgets_page(request: Request) -> HTMLResponse:
    """Render the widget editor: add form + delete-buttons for existing rows."""
    async with get_connection() as conn:
        widgets = await _list_widgets(conn)
    log.info("dashboard_widgets.editor", count=len(widgets))
    return templates.TemplateResponse(
        request,
        "dashboard_widgets.html",
        {
            "title": "Dashboard widgets",
            "active_nav": "settings",
            "widgets": widgets,
            "title_max": TITLE_MAX,
            "query_max": QUERY_MAX,
        },
    )


@router.post("/settings/dashboard-widgets")
async def dashboard_widgets_create(
    title: str = Form(...),
    query: str = Form(...),
) -> RedirectResponse:
    """Persist a new widget at the end of the list."""
    title_v = _validate_title(title)
    query_v = _validate_query(query)

    async with get_connection() as conn:
        position = await _next_position(conn)
        await conn.execute(
            "INSERT INTO dashboard_widget (title, query, position) VALUES (?, ?, ?)",
            (title_v, query_v, position),
        )
        await conn.commit()

    log.info(
        "dashboard_widgets.created",
        title=title_v,
        position=position,
    )
    return RedirectResponse(url="/settings/dashboard-widgets", status_code=303)


@router.post("/settings/dashboard-widgets/{widget_id}/delete")
async def dashboard_widgets_delete(widget_id: int) -> RedirectResponse:
    """Remove a widget by primary key (no-op when missing).

    Path-parameter ``widget_id`` is typed as ``int`` so FastAPI 422s
    any non-integer path before our code runs — that's the only guard
    we need against URL fuzzing here.
    """
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM dashboard_widget WHERE id = ?",
            (widget_id,),
        )
        await conn.commit()
    log.info("dashboard_widgets.deleted", widget_id=widget_id)
    return RedirectResponse(url="/settings/dashboard-widgets", status_code=303)
