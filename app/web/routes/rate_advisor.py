"""Capture-rate advisor UI + JSON API (v1.41).

Surfaces :mod:`app.rate_advisor` over HTTP:

* ``GET  /settings/rate-advisor`` — full-page UI with current
  settings, the last advisor run (if any), the freshly-computed
  recommendation card, and an Apply / Dismiss control pair.
* ``POST /api/rate-advisor/run`` — recompute the advisor state,
  persist a new ``rate_advisor_run`` row, return the result as JSON.
* ``POST /api/rate-advisor/apply/{run_id}`` — apply a previously
  recorded suggestion. Returns 200 + the applied values, or 404 if
  the row id does not exist.
* ``GET  /api/rate-advisor/history.json`` — last 10 runs, newest
  first, JSON shape matches :func:`app.rate_advisor.list_recent_runs`.

The page-render path also runs ``compute_advisor_state`` (without
persisting) so the user always sees a fresh recommendation on every
page load; persistence happens only when they click Run. That keeps
the table free of churn while preserving "open the page, see fresh
numbers" UX.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.rate_advisor import (
    apply_suggestion,
    compute_advisor_state,
    list_recent_runs,
    record_advisor_run,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["rate-advisor"])
log = get_logger("persona.web.rate_advisor")

_HISTORY_LIMIT = 10


@router.get("/settings/rate-advisor", response_class=HTMLResponse)
async def rate_advisor_page(request: Request) -> HTMLResponse:
    """Render the advisor page with a fresh (non-persisted) recommendation."""
    try:
        preview = await compute_advisor_state()
    except Exception as exc:
        log.warning("rate_advisor.preview_failed", error=str(exc))
        preview = None
    history = await list_recent_runs(limit=_HISTORY_LIMIT)
    last_run: dict[str, Any] | None = history[0] if history else None
    return templates.TemplateResponse(
        request,
        "rate_advisor.html",
        {
            "title": "Совет по частоте",
            "active_nav": "settings",
            "preview": preview,
            "last_run": last_run,
            "history": history,
        },
    )


@router.post("/api/rate-advisor/run")
async def rate_advisor_run() -> JSONResponse:
    """Compute, persist, and return a fresh advisor recommendation."""
    state = await compute_advisor_state()
    run_id = await record_advisor_run(state)
    payload: dict[str, Any] = {"run_id": run_id, **state}
    return JSONResponse(payload)


@router.post("/api/rate-advisor/apply/{run_id}")
async def rate_advisor_apply(run_id: int) -> JSONResponse:
    """Apply the suggestion stored under ``run_id`` and return the new values."""
    try:
        result = await apply_suggestion(run_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(result, status_code=200)


@router.get("/api/rate-advisor/history.json")
async def rate_advisor_history() -> JSONResponse:
    """Return the most recent advisor runs as JSON, newest first."""
    history = await list_recent_runs(limit=_HISTORY_LIMIT)
    return JSONResponse({"runs": history})


__all__ = ["router"]
