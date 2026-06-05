"""Operator UI + JSON endpoints for the DB integrity quick-check feature (v1.51).

Surfaces:

* ``GET  /admin/db-integrity``                    — HTML dashboard:
    last 20 check runs (timestamp, kind, status, duration, db size),
    three action buttons (quick / full / analyze).
* ``POST /api/db-integrity/quick-check``          — Trigger
    :func:`app.db_integrity.run_quick_check` off-cycle. Returns the
    coroutine's full result dict.
* ``POST /api/db-integrity/full-check``           — Trigger
    :func:`app.db_integrity.run_full_check`. Slow; the only producer
    of ``check_kind = 'full'`` rows.
* ``POST /api/db-integrity/analyze``              — Trigger
    :func:`app.db_integrity.run_analyze`. Refreshes query-planner
    stats — not an integrity check, just bundled with the others.
* ``GET  /api/db-integrity/history.json``         — Last 20 rows as
    JSON, for the live-refresh button on the page.

All SQL is parametrised. Mutation endpoints return JSON so the page
can call them via ``fetch`` and re-render without a full reload.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.db_integrity import (
    list_recent_runs,
    run_analyze,
    run_full_check,
    run_quick_check,
)
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["db-integrity"])

log = get_logger("persona.web.db_integrity")

#: How many recent runs the dashboard lists. Matches the default the
#: history.json endpoint hands out so the table and the JSON dump
#: agree without the operator having to pick a number.
_RECENT_RUNS_LIMIT: int = 20


@router.get("/admin/db-integrity", response_class=HTMLResponse)
async def db_integrity_page(request: Request) -> HTMLResponse:
    """Render the operator dashboard for DB integrity checks."""
    runs = await list_recent_runs(limit=_RECENT_RUNS_LIMIT)
    log.info("db_integrity.page", recent_runs=len(runs))
    return templates.TemplateResponse(
        request,
        "db_integrity.html",
        {
            "title": "DB integrity",
            "active_nav": "settings",
            "runs": runs,
        },
    )


@router.post("/api/db-integrity/quick-check")
async def db_integrity_quick_check() -> JSONResponse:
    """Trigger one off-cycle :func:`app.db_integrity.run_quick_check`."""
    log.info("db_integrity.quick_check.start")
    result = await run_quick_check()
    log.info(
        "db_integrity.quick_check.done",
        status=result.get("status"),
        duration_ms=result.get("duration_ms", 0),
    )
    return JSONResponse({"ok": True, "result": dict(result)})


@router.post("/api/db-integrity/full-check")
async def db_integrity_full_check() -> JSONResponse:
    """Trigger :func:`app.db_integrity.run_full_check` (slow, operator-only)."""
    log.info("db_integrity.full_check.start")
    result = await run_full_check()
    log.info(
        "db_integrity.full_check.done",
        status=result.get("status"),
        duration_ms=result.get("duration_ms", 0),
    )
    return JSONResponse({"ok": True, "result": dict(result)})


@router.post("/api/db-integrity/analyze")
async def db_integrity_analyze() -> JSONResponse:
    """Trigger :func:`app.db_integrity.run_analyze` (refresh planner stats)."""
    log.info("db_integrity.analyze.start")
    result = await run_analyze()
    log.info(
        "db_integrity.analyze.done",
        status=result.get("status"),
        duration_ms=result.get("duration_ms", 0),
    )
    return JSONResponse({"ok": True, "result": dict(result)})


@router.get("/api/db-integrity/history.json")
async def db_integrity_history_json() -> JSONResponse:
    """Return the most recent check runs as a JSON array."""
    runs = await list_recent_runs(limit=_RECENT_RUNS_LIMIT)
    return JSONResponse({"ok": True, "items": runs})


__all__ = ["router"]
