"""Admin page + endpoint for re-OCR'ing the N most recent screenshots (v0.69).

The operator types a number (default 100, capped at :data:`MAX_RERUN_N`)
and clicks the scary red button. Every one of the N most recently
captured screenshots flips back to ``ocr_status = 'pending'`` so the OCR
worker re-reads them on its next sweep.

Why this exists (alongside :mod:`app.web.routes.ocr_retry` and
:mod:`app.web.routes.ocr_admin`):

* ``ocr_admin`` resets the **whole** ``skipped`` / ``failed`` bucket, no
  notion of recency. Too blunt when you only want to validate a new
  Tesseract language pack against your last few hundred shots.
* ``ocr_retry`` resets specific rows (empty / low-conf) the user ticks.
  Targets quality, not recency.
* ``ocr_rerun_n`` is the recency knob — "re-OCR the last N captures, I
  don't care about their current status." Useful right after rotating
  language packs, swapping Tesseract versions, or installing trained data
  for a new script: you almost always want to verify the change against
  your most recent activity, not a random historical slice.

Schema note
-----------
The task brief asked for ``ocr_done=0``, but the canonical schema
(``app/storage/schema.sql``) uses an ``ocr_status`` enum
(``pending`` / ``done`` / ``skipped`` / ``failed``) — the OCR worker only
picks up rows where ``ocr_status = 'pending'``. Writing ``ocr_done=0``
would be a no-op (no such column) so we honour the existing convention
here. The user-visible semantics ("re-OCR these rows") match exactly.

Safety
------
* ``N`` is bound via ``?`` placeholder, never interpolated.
* ``N`` is clamped to ``[1, MAX_RERUN_N]`` server-side so a tampered form
  value (browser devtools, curl) cannot widen the blast radius.
* Only rows with a non-NULL ``thumbnail_path`` are touched — without an
  image on disk the worker has nothing to re-read.
* Every successful POST writes an :mod:`app.audit` row with the requested
  N, the affected count and the resolved actor, so an operator can trace
  who hit the button if the corpus ever looks suddenly empty.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.audit import log_action
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.web.templates_engine import templates

router = APIRouter(tags=["ocr-rerun-n"])
log = get_logger("persona.ocr.rerun_n")

# Defaults + ceiling for the form. ``MAX_RERUN_N`` matches the brief and
# keeps a single click well below the size at which an ``UPDATE`` would
# stall the worker queue or starve other writers.
DEFAULT_RERUN_N: int = 100
MAX_RERUN_N: int = 5000


def _clamp_n(value: int) -> int:
    """Force ``N`` into ``[1, MAX_RERUN_N]``.

    Anything ``<= 0`` collapses to ``1`` (a no-op N=0 update would still
    write an audit row, which would be confusing in the log); values
    above the cap clamp down to ``MAX_RERUN_N``.
    """
    if value < 1:
        return 1
    if value > MAX_RERUN_N:
        return MAX_RERUN_N
    return value


@router.get("/admin/ocr-rerun-n", response_class=HTMLResponse)
async def ocr_rerun_n_page(
    request: Request,
    affected: int | None = Query(default=None, ge=0),
    requested: int | None = Query(default=None, ge=0),
) -> HTMLResponse:
    """Render the rerun-last-N form with the current defaults + cap.

    ``affected`` / ``requested`` are populated by the POST redirect so
    the page can show a confirmation banner ("re-queued X of Y rows")
    without server-side flash state.
    """
    return templates.TemplateResponse(
        request,
        "ocr_rerun_n.html",
        {
            "title": "OCR rerun last N",
            "active_nav": "settings",
            "default_n": DEFAULT_RERUN_N,
            "max_n": MAX_RERUN_N,
            "affected": affected,
            "requested": requested,
        },
    )


@router.post("/admin/ocr-rerun-n")
async def ocr_rerun_n_submit(
    request: Request,
    n: int = Form(default=DEFAULT_RERUN_N),
) -> RedirectResponse:
    """Reset the ``N`` most recent shots back to ``ocr_status = 'pending'``.

    Recency is decided by ``captured_at DESC`` (matching the timeline /
    grid / archive views — what the user thinks of as "the last N"). The
    ``id DESC`` tiebreaker keeps the result deterministic across two
    shots captured in the same UTC second.

    Returns a 303 redirect back to the form with a ``?affected=N`` query
    string so the page can render a confirmation banner without keeping
    server-side flash state.
    """
    requested = _clamp_n(int(n))

    # Subquery picks the N most-recent shots that actually have an image
    # on disk — without ``thumbnail_path`` the OCR worker has nothing to
    # read, and resetting such a row would just churn it back into
    # ``skipped`` on the next sweep.
    sql = (
        "UPDATE screenshots SET ocr_status = 'pending' "
        "WHERE id IN ("
        "    SELECT id FROM screenshots "
        "    WHERE thumbnail_path IS NOT NULL "
        "    ORDER BY captured_at DESC, id DESC "
        "    LIMIT ?"
        ")"
    )

    async with get_connection() as conn:
        cursor = await conn.execute(sql, (requested,))
        await conn.commit()
        affected = cursor.rowcount or 0

    actor = request.client.host if request.client is not None else None

    log.info(
        "ocr.rerun_n.requeue",
        requested=requested,
        affected=affected,
        actor=actor,
    )
    await log_action(
        action="ocr.rerun_n",
        actor=actor,
        target="screenshots",
        detail=f"requested={requested} affected={affected}",
        success=True,
    )

    return RedirectResponse(
        url=f"/admin/ocr-rerun-n?affected={affected}&requested={requested}",
        status_code=303,
    )


__all__ = ["DEFAULT_RERUN_N", "MAX_RERUN_N", "router"]
