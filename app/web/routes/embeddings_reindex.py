"""Admin page + endpoints for bulk re-indexing screenshot embeddings.

Three surfaces:

* ``GET  /admin/embeddings-reindex``           — Tailwind page with the
  start button and a live progress widget driven by a JS poller.
* ``POST /admin/embeddings-reindex/start``     — kicks the job off in
  the background (``asyncio.create_task``) and redirects back to the
  page. Refuses to start a second job while one is in flight.
* ``GET  /api/embeddings-reindex/status``      — JSON snapshot of the
  module-global progress dict, polled by the page every 2 seconds.

Progress lives in a module-global dict because the job is single-tenant
admin tooling — at most one re-index runs at a time per app process, so
we don't need a cross-process broker. If the process restarts mid-run
the dict resets to ``idle`` and the operator simply clicks again.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.embeddings import is_available
from app.embeddings_reindex import HARD_MAX_SHOTS, reindex_all
from app.logging_setup import get_logger
from app.settings import get_settings
from app.web.templates_engine import templates

router = APIRouter(tags=["embeddings-reindex"])
log = get_logger("persona.embeddings.reindex")

# Defaults for the admin form. ``batch_size`` mirrors
# :data:`app.embeddings_reindex.reindex_all` and ``max_shots`` matches
# the task spec's 10k ceiling — both are clamped server-side.
DEFAULT_BATCH_SIZE: int = 200
DEFAULT_MAX_SHOTS: int = 10_000

# Job statuses surfaced to the UI. ``error`` carries a short string in
# ``_progress["error"]``; ``done`` is terminal until the operator
# starts a new job.
STATUS_IDLE: str = "idle"
STATUS_RUNNING: str = "running"
STATUS_DONE: str = "done"
STATUS_ERROR: str = "error"

# Module-global progress. The route layer is the only writer; the
# background task hands values in through ``_record_progress`` /
# ``_finalise``. Reads from the status endpoint are intentionally
# unsynchronised — dict reads are atomic under CPython and a slightly
# stale snapshot is fine for a 2-second polling UI.
_progress: dict[str, Any] = {
    "status": STATUS_IDLE,
    "processed": 0,
    "total": 0,
    "batches": 0,
    "error": None,
    "started_at": None,
    "finished_at": None,
}

# Holds the running task so we can introspect ``done()`` for races
# between the form POST and the previous job's last batch. Wrapped in a
# mutable dict so the route handlers don't need a ``global`` declaration
# (ruff PLW0603) when swapping the reference.
_task_holder: dict[str, asyncio.Task[dict[str, int]] | None] = {"task": None}


def _clamp(value: int, lo: int, hi: int) -> int:
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def _record_progress(processed: int, total: int) -> None:
    """Progress callback handed to :func:`reindex_all`.

    Keeps the existing ``status`` (the background runner manages
    transitions) and only nudges the counters.
    """
    _progress["processed"] = processed
    _progress["total"] = total


def _is_job_active() -> bool:
    """True if a re-index task is currently in flight."""
    task = _task_holder["task"]
    return task is not None and not task.done()


async def _run_job(batch_size: int, max_shots: int) -> dict[str, int]:
    """Background entry point: drive ``reindex_all`` and update progress."""
    _progress["status"] = STATUS_RUNNING
    _progress["processed"] = 0
    _progress["total"] = 0
    _progress["batches"] = 0
    _progress["error"] = None
    _progress["started_at"] = datetime.now(UTC).isoformat()
    _progress["finished_at"] = None

    try:
        summary = await reindex_all(
            batch_size=batch_size,
            max_shots=max_shots,
            progress=_record_progress,
        )
    except asyncio.CancelledError:
        _progress["status"] = STATUS_ERROR
        _progress["error"] = "cancelled"
        _progress["finished_at"] = datetime.now(UTC).isoformat()
        log.warning("embeddings.reindex.route.cancelled")
        raise
    except Exception as exc:
        _progress["status"] = STATUS_ERROR
        _progress["error"] = str(exc)
        _progress["finished_at"] = datetime.now(UTC).isoformat()
        log.exception("embeddings.reindex.route.failed", error=str(exc))
        return {"processed": 0, "total": 0, "batches": 0}

    _progress["status"] = STATUS_DONE
    _progress["processed"] = summary["processed"]
    _progress["total"] = summary["total"]
    _progress["batches"] = summary["batches"]
    _progress["finished_at"] = datetime.now(UTC).isoformat()
    log.info(
        "embeddings.reindex.route.done",
        processed=summary["processed"],
        total=summary["total"],
        batches=summary["batches"],
    )
    return summary


@router.get("/admin/embeddings-reindex", response_class=HTMLResponse)
async def embeddings_reindex_page(request: Request) -> HTMLResponse:
    """Render the admin page with the start button and progress widget."""
    settings = get_settings()
    return templates.TemplateResponse(
        request,
        "embeddings_reindex.html",
        {
            "title": "Embeddings re-index",
            "active_nav": "settings",
            "enabled": settings.embeddings_enabled,
            "library_available": is_available(),
            "model": settings.embeddings_model,
            "default_batch_size": DEFAULT_BATCH_SIZE,
            "default_max_shots": DEFAULT_MAX_SHOTS,
            "hard_max_shots": HARD_MAX_SHOTS,
            "is_running": _is_job_active(),
        },
    )


@router.post("/admin/embeddings-reindex/start")
async def embeddings_reindex_start(request: Request) -> RedirectResponse:
    """Spawn a background re-index task and redirect back to the page.

    No-ops if a job is already in flight — the UI shows the in-progress
    state so a double-click is visually obvious.
    """
    if _is_job_active():
        log.info("embeddings.reindex.route.start.skipped_already_running")
        return RedirectResponse(url="/admin/embeddings-reindex", status_code=303)

    form = await request.form()

    batch_raw = str(form.get("batch_size") or DEFAULT_BATCH_SIZE)
    max_raw = str(form.get("max_shots") or DEFAULT_MAX_SHOTS)
    try:
        batch_size = _clamp(int(batch_raw), 1, 1000)
    except ValueError:
        batch_size = DEFAULT_BATCH_SIZE
    try:
        max_shots = _clamp(int(max_raw), 1, HARD_MAX_SHOTS)
    except ValueError:
        max_shots = DEFAULT_MAX_SHOTS

    log.info(
        "embeddings.reindex.route.start",
        batch_size=batch_size,
        max_shots=max_shots,
    )

    _task_holder["task"] = asyncio.create_task(_run_job(batch_size, max_shots))
    return RedirectResponse(url="/admin/embeddings-reindex", status_code=303)


@router.get("/api/embeddings-reindex/status", response_class=JSONResponse)
async def embeddings_reindex_status() -> JSONResponse:
    """JSON snapshot of the current job state — polled by the page."""
    total = int(_progress["total"]) if _progress["total"] else 0
    processed = int(_progress["processed"]) if _progress["processed"] else 0
    progress_ratio = round(processed / total, 4) if total else 0.0

    return JSONResponse(
        {
            "status": _progress["status"],
            "processed": processed,
            "total": total,
            "batches": int(_progress["batches"] or 0),
            "progress": progress_ratio,
            "error": _progress["error"],
            "started_at": _progress["started_at"],
            "finished_at": _progress["finished_at"],
            "is_running": _is_job_active(),
        }
    )
