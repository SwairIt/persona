"""Inline OCR text editor endpoint (v0.94 feature 3/3).

Sister write-path to the v0.77 bulk find-and-replace
(:mod:`app.ocr_find_replace`) and the v0.75 vision-text promotion
(:mod:`app.web.routes.ocr_vision_replace`). Both overwrite
``screenshots.ocr_text`` and both — since v0.92 — snapshot the
about-to-be-overwritten value into :mod:`app.ocr_history` so the
operator can revert.

Until v0.94 there was no UI for editing a single shot's OCR text by
hand: regex find-and-replace covered systemic Tesseract errors and the
vision-replace button covered "the model read it better than tesseract
did", but neither helped with a one-off mis-recognition the operator
wants to correct on the screenshot detail page in front of them. This
module closes that gap with a single endpoint:

* ``POST /api/screenshot/{id}/ocr`` — form field ``text``. Snapshots
  the current ``ocr_text`` into :mod:`app.ocr_history` (``reason="manual"``),
  writes the new text via parametrised UPDATE, then issues an explicit
  FTS5 ``rebuild`` so the search index reflects the edit even if a
  future refactor drops the ``screenshots_au`` trigger.

Design contract
---------------
* **Snapshot first, write second.** The same ordering used by
  ``ocr_find_replace.apply`` and ``ocr_vision_replace.apply`` so a
  revert always restores the *previous* text, never the new one.
* **Empty/whitespace input is allowed but skipped-as-snapshot when the
  current text is also empty.** :func:`app.ocr_history.record_snapshot`
  treats ``None``/``""`` as a silent no-op so blanking an already-blank
  OCR field never grows the history table.
* **404 on unknown shot.** A missing row is mapped to 404 (helper
  returns ``None``-ish) so a stale UI doesn't get a misleading 500.
* **Audit-logged.** Each successful POST writes an :mod:`app.audit`
  row keyed off the same ``ocr.manual_edit`` action slug so a
  security review can grep one slug to find every manual OCR edit.
* **Parametrised SQL.** The UPDATE binds both values via ``?``
  placeholders; the FTS rebuild is a static control statement with no
  interpolated values.
* **structlog ``persona.ocr_edit``.** Every call emits a structured
  log line so the snapshot/update/audit timeline is reconstructable
  without consulting the DB directly.
"""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse

from app.audit import log_action
from app.logging_setup import get_logger
from app.ocr_history import record_snapshot
from app.storage.db import get_connection

router = APIRouter(tags=["ocr-edit"])
log = get_logger("persona.ocr_edit")


@router.post("/api/screenshot/{screenshot_id}/ocr")
async def screenshot_ocr_edit(
    screenshot_id: int,
    request: Request,
    text: str = Form(default=""),
) -> JSONResponse:
    """Replace ``screenshots.ocr_text`` for ``screenshot_id`` with ``text``.

    Workflow:

    1. SELECT the current ``ocr_text`` so the v0.92 history snapshot
       captures the *pre-edit* value (or skips when there's nothing
       worth reverting to).
    2. UPDATE the row with the new ``text`` payload. The
       ``screenshots_au`` trigger keeps ``screenshots_fts`` in sync per
       UPDATE; we mirror the explicit rebuild ``ocr_find_replace`` uses
       as a belt-and-braces safety net.
    3. Write an :mod:`app.audit` row keyed off ``ocr.manual_edit`` so
       the security-review trail stays uniform with the other
       privileged OCR write paths.

    Args:
        screenshot_id: ``screenshots.id`` of the shot whose OCR text
            should be replaced. A missing id yields 404.
        request: Used only to derive the actor (client host) for the
            audit row.
        text: New OCR body. May be empty (the editor allows clearing
            the field); leading/trailing whitespace is preserved
            verbatim so the operator can intentionally pad with
            newlines.

    Returns:
        ``JSONResponse`` of the form
        ``{"ok": true, "shot_id": int, "history_id": int | None, "chars": int}``.
        ``history_id`` is ``null`` when the previous text was empty
        and therefore not worth snapshotting.
    """
    actor = request.client.host if request.client is not None else None
    new_text = text or ""

    async with get_connection() as conn:
        # Look up the current text before overwriting it. We need the
        # exact pre-edit value for the v0.92 snapshot; a missing row
        # short-circuits to a 404 so a stale UI doesn't get a confusing
        # success response.
        cursor = await conn.execute(
            "SELECT ocr_text FROM screenshots WHERE id = ?",
            (int(screenshot_id),),
        )
        row = await cursor.fetchone()
        if row is None:
            await log_action(
                action="ocr.manual_edit",
                actor=actor,
                target=f"screenshots.id={screenshot_id}",
                detail="not_found",
                success=False,
            )
            log.warning(
                "ocr_edit.api.not_found",
                screenshot_id=screenshot_id,
                actor=actor,
            )
            raise HTTPException(status_code=404, detail="Screenshot not found")

        raw_prev = row["ocr_text"]
        prev_text: str | None = None if raw_prev is None else str(raw_prev)

    # ``record_snapshot`` opens its own connection (same underlying file
    # handle, serialised by SQLite's lock). It silently no-ops on
    # NULL/empty bodies, so editing a shot that previously had no OCR
    # text never grows the history table with a non-revertible row.
    history_id = await record_snapshot(
        int(screenshot_id), prev_text, reason="manual"
    )

    async with get_connection() as conn:
        await conn.execute(
            "UPDATE screenshots SET ocr_text = ? WHERE id = ?",
            (new_text, int(screenshot_id)),
        )
        # Belt-and-braces FTS refresh — see :mod:`app.ocr_find_replace`
        # module docstring. This statement is a SQLite FTS5 control
        # command, not a row insert; no values are interpolated.
        await conn.execute(
            "INSERT INTO screenshots_fts(screenshots_fts) VALUES('rebuild')"
        )
        await conn.commit()

    await log_action(
        action="ocr.manual_edit",
        actor=actor,
        target=f"screenshots.id={screenshot_id}",
        detail=(
            f"history_id={history_id} "
            f"prev_chars={len(prev_text) if prev_text else 0} "
            f"new_chars={len(new_text)}"
        ),
        success=True,
    )
    log.info(
        "ocr_edit.api.ok",
        screenshot_id=int(screenshot_id),
        history_id=history_id,
        prev_chars=len(prev_text) if prev_text else 0,
        new_chars=len(new_text),
        actor=actor,
    )

    return JSONResponse(
        {
            "ok": True,
            "shot_id": int(screenshot_id),
            "history_id": history_id,
            "chars": len(new_text),
        }
    )


__all__ = ["router"]
