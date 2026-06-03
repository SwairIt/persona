"""Per-day audit timeline — chronological vertical view of one day's events.

v0.76 feature 3/3. Adds two endpoints, both keyed off the local calendar
day stored in the ``audit_log`` table (created in
``037_audit_log.sql``):

    * ``GET /audit/timeline/{day}``           — renders
      ``audit_timeline.html``. Every audit row receives an
      ``id="audit-{id}"`` anchor so callers can deep-link straight to a
      single event inside the timeline.
    * ``GET /api/audit/timeline/{day}.json``  — machine-readable
      equivalent for tooling, dashboards and any future automation.

The ``day`` path component is ``YYYY-MM-DD``. A malformed value falls
back to *today* — same forgiving behaviour as the day-scrubber,
day-kanban and notes-timeline routes; punishing typos on an exploratory
read-only view is user-hostile.

All SQL is parametrised — both queries here filter on the literal
``YYYY-MM-DD`` string via SQLite's ``date(ts)`` function and bind the
day value with a ``?`` placeholder, so the user-supplied path component
is never spliced into the statement.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main`; the task spec forbids touching ``main.py``. Wire
it up in a follow-up patch with::

    from app.web.routes import audit_timeline as audit_timeline_routes
    app.include_router(audit_timeline_routes.router)
"""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Final

import aiosqlite
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

if TYPE_CHECKING:
    from collections.abc import Sequence

log = get_logger("persona.audit_timeline")

router = APIRouter(tags=["audit-timeline"])

# Hard cap on audit rows returned for a single day. A noisy day (lots of
# vault reads, bulk-delete batches) is still well under this ceiling;
# anything past it is almost certainly noise and would also kill the
# browser by inflating the DOM beyond comfortable scrolling.
_MAX_ROWS_PER_DAY: Final[int] = 1_000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _today_local() -> date:
    """Local-date "today" — matches what the wall clock + other day-views show."""
    return datetime.now().astimezone().date()


def _parse_day_or_today(day: str | None) -> date:
    """Parse ``YYYY-MM-DD``; fall back to local today on any failure.

    Matches the day-scrubber / day-kanban / notes-timeline convention: a
    bad path lands on today rather than 400-ing. A timeline is
    exploratory — surfacing *something* useful beats a stack trace.
    """
    if day is None or day == "":
        return _today_local()
    try:
        return datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        log.info("audit.timeline.day_invalid_fallback_today", value=day)
        return _today_local()


def _project_row(row: Any) -> dict[str, Any]:
    """Build a single timeline row dict (used by both HTML + JSON).

    Mirrors the public :class:`app.audit.AuditRow` shape — same field
    names, same NULL-vs-empty semantics — so a caller already wired up
    to ``/api/audit.json`` can reuse its parsing for the per-day variant
    without a separate code path.
    """
    return {
        "id": int(row["id"]),
        "ts": str(row["ts"]),
        "action": str(row["action"]),
        "actor": (None if row["actor"] is None else str(row["actor"])),
        "target": (None if row["target"] is None else str(row["target"])),
        "detail": (None if row["detail"] is None else str(row["detail"])),
        "success": bool(int(row["success"])),
    }


async def _load_day_rows(day_value: date) -> list[dict[str, Any]]:
    """Fetch every audit row whose ``date(ts) = day_value``.

    Uses SQLite's ``date(...)`` function on the stored ISO timestamp so
    the filter matches the same wall-clock day the audit row itself was
    written against (rows are inserted via ``datetime('now')`` —
    SQLite emits UTC there, but day-grouping by the same ``date(...)``
    keeps the query self-consistent regardless of the user's tz).

    Ordering is ascending by ``ts`` then ``id`` so the timeline reads
    top-down chronologically — the opposite of the paginated table on
    ``/audit``, which is intentionally newest-first for "what just
    happened" review. A timeline is a story; it tells better forwards.

    Errors swallowed and surfaced as an empty list, matching
    :func:`app.audit.list_recent` — a transient SQLite hiccup should
    render an empty timeline, not 500 the page.
    """
    day_str = day_value.strftime("%Y-%m-%d")
    # Fully-static SQL string; the day value travels via the ``?`` bind.
    sql = (
        "SELECT id, ts, action, actor, target, detail, success "
        "FROM audit_log "
        "WHERE date(ts) = ? "
        "ORDER BY ts ASC, id ASC "
        "LIMIT ?"
    )
    params: Sequence[object] = (day_str, _MAX_ROWS_PER_DAY)
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(sql, params)
            rows = await cursor.fetchall()
    except aiosqlite.Error as exc:
        log.warning(
            "audit.timeline.load_failed",
            day=day_value.isoformat(),
            error=str(exc),
        )
        return []
    return [_project_row(row) for row in rows]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/audit/timeline/{day}", response_class=HTMLResponse)
async def audit_timeline_page(request: Request, day: str) -> HTMLResponse:
    """Render the per-day audit timeline as HTML.

    Each row receives ``id="audit-{id}"`` so callers can deep-link to a
    specific event by appending ``#audit-42`` to the URL.
    """
    day_value = _parse_day_or_today(day)
    items = await _load_day_rows(day_value)
    log.info(
        "audit.timeline.page",
        day=day_value.isoformat(),
        count=len(items),
    )
    return templates.TemplateResponse(
        request,
        "audit_timeline.html",
        {
            "title": f"Audit timeline — {day_value.isoformat()}",
            "active_nav": "settings",
            "day": day_value.isoformat(),
            # Keep the context key off ``items`` — ``base.html`` does
            # ``{% set items = [...] %}`` for its nav and would shadow
            # our list, silently rendering an empty timeline.
            "rows": items,
            "total": len(items),
            "truncated": len(items) >= _MAX_ROWS_PER_DAY,
        },
    )


@router.get(
    "/api/audit/timeline/{day}.json",
    response_class=JSONResponse,
)
async def audit_timeline_json(day: str) -> JSONResponse:
    """Machine-readable companion to :func:`audit_timeline_page`.

    Shape per item: ``{id, ts, action, actor, target, detail, success}``
    — identical to :class:`app.audit.AuditRow` so existing clients of
    ``/api/audit.json`` can consume this endpoint with zero changes.
    """
    day_value = _parse_day_or_today(day)
    items = await _load_day_rows(day_value)
    log.info(
        "audit.timeline.json",
        day=day_value.isoformat(),
        count=len(items),
    )
    return JSONResponse(
        {
            "day": day_value.isoformat(),
            "total": len(items),
            "truncated": len(items) >= _MAX_ROWS_PER_DAY,
            "items": items,
        }
    )


__all__ = ["router"]
