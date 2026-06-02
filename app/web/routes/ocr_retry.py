"""Admin page and endpoints for the OCR retry queue.

Surfaces screenshots whose OCR pass produced an empty or low-confidence
result and lets the operator re-queue them in bulk (set
``ocr_status='pending'`` so the OCR worker picks them up again).

Three filter modes drive the page:

* ``both`` (default) — empty OCR results + sub-threshold confidence rows
* ``empty`` — only rows with ``ocr_text IS NULL`` or ``''``
* ``low`` — only rows whose stored per-word confidence averages below
  ``min_conf``

Two POST endpoints write:

* ``/admin/ocr-retry/requeue`` — re-queues a list of ids the user ticked.
* ``/admin/ocr-retry/requeue-all-shown`` — re-queues every row matching the
  current filter, capped at :data:`app.ocr_retry.MAX_LIMIT` to avoid a
  runaway UPDATE.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.logging_setup import get_logger
from app.ocr_retry import (
    DEFAULT_MIN_CONF,
    MAX_LIMIT,
    count_problem_shots,
    list_problem_shots,
    requeue_matching,
    requeue_shots,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["ocr-retry"])
log = get_logger("persona.ocr.retry")

# Page size for the table. Stays well under MAX_LIMIT so a render never
# materialises a six-figure result set.
PAGE_LIMIT: int = 200

FilterMode = Literal["both", "empty", "low"]


def _normalise_mode(value: str | None) -> FilterMode:
    """Coerce the ``mode`` query/form value into one of the three valid modes."""
    if value == "empty":
        return "empty"
    if value == "low":
        return "low"
    return "both"


def _mode_flags(mode: FilterMode) -> tuple[bool, bool]:
    """Translate the public ``mode`` into the ``(only_empty, only_low)`` pair."""
    if mode == "empty":
        return True, False
    if mode == "low":
        return False, True
    return False, False


def _clamp_conf(value: int) -> int:
    if value < 0:
        return 0
    if value > 100:
        return 100
    return value


@router.get("/admin/ocr-retry", response_class=HTMLResponse)
async def ocr_retry_page(
    request: Request,
    mode: str = Query(default="both"),
    min_conf: int = Query(default=DEFAULT_MIN_CONF, ge=0, le=100),
) -> HTMLResponse:
    """Render the OCR retry queue with the current filter applied."""
    chosen_mode = _normalise_mode(mode)
    only_empty, only_low = _mode_flags(chosen_mode)
    capped_conf = _clamp_conf(min_conf)

    rows = await list_problem_shots(
        limit=PAGE_LIMIT,
        min_conf=capped_conf,
        only_empty=only_empty,
        only_low=only_low,
    )
    total = await count_problem_shots(
        min_conf=capped_conf,
        only_empty=only_empty,
        only_low=only_low,
    )
    return templates.TemplateResponse(
        request,
        "ocr_retry.html",
        {
            "title": "OCR retry queue",
            "active_nav": "settings",
            "rows": rows,
            "total": total,
            "shown": len(rows),
            "mode": chosen_mode,
            "min_conf": capped_conf,
            "page_limit": PAGE_LIMIT,
            "max_limit": MAX_LIMIT,
        },
    )


@router.post("/admin/ocr-retry/requeue")
async def ocr_retry_requeue(request: Request) -> RedirectResponse:
    """Re-queue the screenshot ids the user ticked in the table.

    The form field name is ``ids`` (one ``<input type="checkbox" name="ids">``
    per row). FastAPI's ``Form(...)`` collapses repeated fields into the
    first value only, so we read the raw form ourselves.
    """
    form = await request.form()
    raw_values = form.getlist("ids")
    ids: list[int] = []
    for value in raw_values:
        try:
            ids.append(int(str(value)))
        except (TypeError, ValueError):
            continue

    mode = _normalise_mode(str(form.get("mode") or ""))
    min_conf_raw = str(form.get("min_conf") or DEFAULT_MIN_CONF)
    try:
        min_conf = _clamp_conf(int(min_conf_raw))
    except ValueError:
        min_conf = DEFAULT_MIN_CONF

    affected = await requeue_shots(ids)
    log.info(
        "ocr.retry.route.requeue",
        requested=len(ids),
        affected=affected,
        mode=mode,
    )
    redirect = f"/admin/ocr-retry?mode={mode}&min_conf={min_conf}"
    return RedirectResponse(url=redirect, status_code=303)


@router.post("/admin/ocr-retry/requeue-all-shown")
async def ocr_retry_requeue_all_shown(request: Request) -> RedirectResponse:
    """Re-queue every row matching the current filter, capped at ``MAX_LIMIT``.

    The cap exists so a single click on a database with 100k empty-OCR rows
    can never overwhelm the worker queue in one transaction.
    """
    form = await request.form()
    mode = _normalise_mode(str(form.get("mode") or ""))
    min_conf_raw = str(form.get("min_conf") or DEFAULT_MIN_CONF)
    try:
        min_conf = _clamp_conf(int(min_conf_raw))
    except ValueError:
        min_conf = DEFAULT_MIN_CONF

    only_empty, only_low = _mode_flags(mode)
    affected = await requeue_matching(
        min_conf=min_conf,
        only_empty=only_empty,
        only_low=only_low,
        cap=MAX_LIMIT,
    )
    log.info(
        "ocr.retry.route.requeue_all_shown",
        affected=affected,
        mode=mode,
        min_conf=min_conf,
    )
    redirect = f"/admin/ocr-retry?mode={mode}&min_conf={min_conf}"
    return RedirectResponse(url=redirect, status_code=303)
