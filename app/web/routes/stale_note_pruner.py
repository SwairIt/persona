"""Operator UI + JSON endpoints for the stale-note pruner (v1.49).

Surfaces:

* ``GET  /admin/stale-notes``                  — HTML dashboard listing
  the current candidate count, last preview run, and "Preview" /
  "Prune now" buttons.
* ``POST /api/stale-notes/preview``            — Trigger one
  :func:`find_stale_notes` scan and return the candidate list as JSON.
  Non-destructive — safe to mash.
* ``POST /api/stale-notes/prune``              — Trigger one
  :func:`prune_stale` run with ``dry_run=False``. Destructive (soft
  delete); the UI button confirms before posting.

All SQL goes through the pruner module (parametrised). Mutation
endpoints return JSON so the page can call them via ``fetch`` and
re-render without a full reload.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.stale_note_pruner import (
    DEFAULT_MIN_AGE_DAYS,
    find_stale_notes,
    prune_stale,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["stale-note-pruner"])

log = get_logger("persona.web.stale_note_pruner")


@router.get("/admin/stale-notes", response_class=HTMLResponse)
async def stale_notes_page(request: Request) -> HTMLResponse:
    """Render the operator dashboard for the stale-note pruner.

    Shows a fresh candidate count (cheap COUNT query under the hood)
    so the operator can see "would I prune anything if I clicked the
    button right now?" without explicitly previewing first.
    """
    candidates = await find_stale_notes(min_age_days=DEFAULT_MIN_AGE_DAYS)
    log.info(
        "stale_note_pruner.page",
        candidate_count=len(candidates),
        age_threshold_days=DEFAULT_MIN_AGE_DAYS,
    )
    return templates.TemplateResponse(
        request,
        "stale_note_pruner.html",
        {
            "title": "Очистка пустых заметок",
            "active_nav": "settings",
            "candidate_count": len(candidates),
            "candidates": candidates[:50],  # cap the rendered list
            "age_threshold_days": DEFAULT_MIN_AGE_DAYS,
        },
    )


@router.post("/api/stale-notes/preview")
async def stale_notes_preview() -> JSONResponse:
    """Return the list of stale-note candidates as JSON.

    Non-destructive: this only reads. The UI calls it from the
    "Preview" button so the operator can audit what would be touched
    before pressing "Prune".
    """
    candidates = await find_stale_notes(min_age_days=DEFAULT_MIN_AGE_DAYS)
    log.info(
        "stale_note_pruner.preview",
        candidate_count=len(candidates),
    )
    return JSONResponse(
        {
            "ok": True,
            "age_threshold_days": DEFAULT_MIN_AGE_DAYS,
            "count": len(candidates),
            "candidates": candidates,
        },
    )


@router.post("/api/stale-notes/prune")
async def stale_notes_prune() -> JSONResponse:
    """Soft-delete the stale notes. Destructive; UI confirms first.

    Returns the pruner result dict so the page can show "X rows soft-
    deleted" without an extra round-trip.
    """
    log.info("stale_note_pruner.prune.start")
    result = await prune_stale(
        min_age_days=DEFAULT_MIN_AGE_DAYS, dry_run=False,
    )
    log.info(
        "stale_note_pruner.prune.done",
        count=result.get("count", 0),
        age_threshold_days=result.get("age_threshold_days", 0),
    )
    return JSONResponse({"ok": True, "result": dict(result)})


__all__ = ["router"]
