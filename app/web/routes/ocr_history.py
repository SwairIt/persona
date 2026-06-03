"""HTTP endpoints surfacing the v0.92 OCR edit-history snapshots.

Two endpoints back the "History" link rendered on the screenshot detail
page (see :file:`screenshot.html`):

* ``GET  /api/screenshot/{id}/ocr-history.json``
    Return ``{"history": [...]}`` — the snapshot rows captured by the
    three write paths that overwrite ``screenshots.ocr_text``
    (:mod:`app.ocr_find_replace`, :mod:`app.web.routes.ocr_vision_replace`
    and any future manual editor). Newest-first so the UI can render a
    short list with revert buttons.
* ``POST /api/ocr-history/{id}/revert``
    Restore one snapshot's ``prev_text`` into the live ``ocr_text``
    column. The :func:`app.ocr_history.revert` helper takes a fresh
    snapshot of the about-to-be-overwritten value first, so reverting a
    revert is just a second click on the new top row.

Both endpoints are audit-logged via :mod:`app.audit` so a security
review can correlate revert actions with the corresponding
``ocr.find_replace`` / ``ocr.vision_replace`` rows.

Design notes
------------
* **JSON payloads, no HTML fragments.** The template ships a tiny
  inline fetcher that materialises the list client-side; keeping the
  endpoints JSON-only avoids coupling the API surface to Jinja and
  matches the v0.87 emails / v0.88 phones siblings.
* **Parametrised SQL via the helper module.** This route layer never
  builds SQL; all DB access goes through :mod:`app.ocr_history`.
* **404 on unknown snapshot.** A missing history id is mapped to 404
  (not 400) so a stale UI that POSTs an already-reverted-and-deleted
  row receives the conventional ``Not Found`` response. The helper
  returns ``None`` rather than raising so the route can decide.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.audit import log_action
from app.logging_setup import get_logger
from app.ocr_history import list_for_shot, revert

router = APIRouter(tags=["ocr-history"])
log = get_logger("persona.ocr_history")


@router.get(
    "/api/screenshot/{screenshot_id}/ocr-history.json",
    response_class=JSONResponse,
)
async def screenshot_ocr_history_json(screenshot_id: int) -> JSONResponse:
    """Return snapshot rows for ``screenshot_id`` in newest-first order."""
    rows = await list_for_shot(screenshot_id)
    log.info(
        "ocr_history.api.list",
        screenshot_id=screenshot_id,
        count=len(rows),
    )
    return JSONResponse({"history": list(rows)})


@router.post("/api/ocr-history/{history_id}/revert")
async def ocr_history_revert(
    history_id: int, request: Request
) -> JSONResponse:
    """Restore one snapshot's ``prev_text`` into the live ``ocr_text``.

    A 404 is returned when ``history_id`` does not exist (helper
    returns ``None`` rather than raising). On success the JSON payload
    mirrors :class:`app.ocr_history.RevertResult` plus an ``ok: true``
    marker so the UI can branch on the boolean without re-parsing.
    """
    actor = request.client.host if request.client is not None else None

    result = await revert(history_id)
    if result is None:
        await log_action(
            action="ocr.history.revert",
            actor=actor,
            target=f"ocr_history.id={history_id}",
            detail="not_found",
            success=False,
        )
        raise HTTPException(status_code=404, detail="History row not found")

    await log_action(
        action="ocr.history.revert",
        actor=actor,
        target=f"screenshots.id={result['shot_id']}",
        detail=(
            f"history_id={result['history_id']} "
            f"restored_chars={result['restored_chars']}"
        ),
        success=True,
    )
    log.info(
        "ocr_history.api.revert.ok",
        history_id=history_id,
        shot_id=result["shot_id"],
        restored_chars=result["restored_chars"],
        actor=actor,
    )
    return JSONResponse(
        {
            "ok": True,
            "history_id": result["history_id"],
            "shot_id": result["shot_id"],
            "restored_chars": result["restored_chars"],
        }
    )


__all__ = ["router"]
