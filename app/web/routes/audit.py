"""Audit log viewer — read-only UI + JSON API (v0.36).

* ``GET /audit``           renders the HTML page with filter + pagination.
* ``GET /api/audit.json``  returns the same rows as JSON for tooling.

The page is read-only by design: there is no "clear log" button and no
delete endpoint. The audit log is append-only — see :mod:`app.audit`.

Filters
-------
``action`` (substring), ``actor`` (substring), ``date_from`` /
``date_to`` (inclusive ISO date bounds, ``YYYY-MM-DD`` or full
``YYYY-MM-DD HH:MM:SS``). All combine via ``AND``; rows are still
sorted newest-first regardless of which filters are active.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.audit import count_recent, list_recent
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["audit"])
log = get_logger("persona.web.audit")
filter_log = get_logger("persona.audit.filter")

# 100 rows / page matches the task spec and gives an at-a-glance density
# without forcing the user to scroll past four screens of table.
_PAGE_SIZE = 100
# Hard ceiling for the ``?page=`` query so an attacker can't issue
# ``?page=10**9`` and force us to compute pointless OFFSET arithmetic.
_PAGE_MAX = 10_000


def _clamp_page(page: int) -> int:
    """Bound the page index to ``1..PAGE_MAX``.

    Page numbers are 1-indexed for UI clarity; the SQL offset is
    derived as ``(page - 1) * PAGE_SIZE``.
    """
    if page < 1:
        return 1
    if page > _PAGE_MAX:
        return _PAGE_MAX
    return page


def _normalise(value: str | None) -> str | None:
    """Trim and collapse blanks to ``None`` so ``?x=`` == omitting ``x``."""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    action: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    page: int = Query(default=1),
) -> HTMLResponse:
    """Render the paginated audit-log table.

    ``action`` / ``actor`` are matched as substrings (SQL ``LIKE %x%``).
    ``date_from`` / ``date_to`` are inclusive ISO bounds on ``ts``. An
    empty string for any field is normalised to ``None`` so URLs like
    ``?action=&actor=`` behave the same as omitting those parameters.
    """
    action_like = _normalise(action)
    actor_like = _normalise(actor)
    from_value = _normalise(date_from)
    to_value = _normalise(date_to)
    safe_page = _clamp_page(page)
    offset = (safe_page - 1) * _PAGE_SIZE

    rows = await list_recent(
        limit=_PAGE_SIZE,
        action_like=action_like,
        actor_like=actor_like,
        date_from=from_value,
        date_to=to_value,
        offset=offset,
    )
    total = await count_recent(
        action_like=action_like,
        actor_like=actor_like,
        date_from=from_value,
        date_to=to_value,
    )
    # Round up — ``(total + PAGE_SIZE - 1) // PAGE_SIZE`` avoids float math.
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    filter_log.info(
        "audit.filter.apply",
        action=action_like,
        actor=actor_like,
        date_from=from_value,
        date_to=to_value,
        page=safe_page,
        matched=total,
    )

    return templates.TemplateResponse(
        request,
        "audit.html",
        {
            "title": "Audit log",
            "active_nav": "settings",
            "rows": rows,
            "filter_action": action_like or "",
            "filter_actor": actor_like or "",
            "filter_date_from": from_value or "",
            "filter_date_to": to_value or "",
            "filters_active": any(
                (action_like, actor_like, from_value, to_value),
            ),
            "page": safe_page,
            "page_size": _PAGE_SIZE,
            "total_rows": total,
            "total_pages": total_pages,
            "has_prev": safe_page > 1,
            "has_next": safe_page < total_pages,
        },
    )


@router.get("/api/audit.json", response_class=JSONResponse)
async def audit_json(
    action: str | None = Query(default=None),
    actor: str | None = Query(default=None),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
    page: int = Query(default=1),
    limit: int = Query(default=_PAGE_SIZE),
) -> JSONResponse:
    """JSON projection of the same rows for tooling / dashboards.

    Mirrors the HTML page's filtering + pagination so a script can
    walk the log in chunks. ``limit`` is honoured up to the hard cap
    enforced inside :func:`app.audit.list_recent`.
    """
    action_like = _normalise(action)
    actor_like = _normalise(actor)
    from_value = _normalise(date_from)
    to_value = _normalise(date_to)
    safe_page = _clamp_page(page)
    safe_limit = max(1, min(int(limit), _PAGE_SIZE))
    offset = (safe_page - 1) * safe_limit

    rows = await list_recent(
        limit=safe_limit,
        action_like=action_like,
        actor_like=actor_like,
        date_from=from_value,
        date_to=to_value,
        offset=offset,
    )
    total = await count_recent(
        action_like=action_like,
        actor_like=actor_like,
        date_from=from_value,
        date_to=to_value,
    )

    return JSONResponse(
        {
            "rows": list(rows),
            "page": safe_page,
            "limit": safe_limit,
            "total": total,
            "action_like": action_like,
            "actor_like": actor_like,
            "date_from": from_value,
            "date_to": to_value,
        }
    )
