"""Single-shot manual OCR re-run.

The batch :mod:`app.ocr_retry` flips many rows back to ``ocr_status =
'pending'`` and lets the background worker drain them. That is the wrong
shape for the common operator request "this *one* screenshot came back
as garbage, run OCR again *now*". This module exposes a single async
entrypoint, :func:`rerun_ocr_for_shot`, that:

1. Loads the screenshots row.
2. Reads the thumbnail file from disk.
3. Runs the Tesseract pipeline inline (the same ``extract_text`` /
   ``redact`` pair used by :mod:`app.workers.ocr_worker`).
4. UPDATEs ``ocr_text`` + ``ocr_status='done'`` + bumps
   ``ocr_rerun_count`` (migration 122).

The result dict carries enough information for the HTTP route — and the
HTMX button on the screenshot detail page — to render a non-ambiguous
outcome without re-querying. ``char_count_before`` / ``char_count_after``
let the UI surface "OCR text changed from 0 → 412 chars" without making
the operator open the diff page.

This module deliberately does NOT touch ``ocr_word`` rows, sentiment,
phrase tags, or the per-shot palette. Those are background side-channels
of the batch worker; re-running them inline would multiply the
button's latency for marginal gain. If the operator wants a *full*
re-process they can flip the row to ``pending`` via the admin page.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, TypedDict

from PIL import Image

from app.logging_setup import get_logger
from app.ocr import OCRNotAvailable, extract_text, redact
from app.settings import get_settings
from app.storage.db import get_connection

log = get_logger("persona.ocr_rerun")

RerunStatus = Literal["ok", "missing_image", "ocr_failed", "shot_not_found"]


class RerunResult(TypedDict):
    """Shape returned by :func:`rerun_ocr_for_shot`.

    ``status``:
        * ``"ok"`` — OCR ran and the row was updated.
        * ``"missing_image"`` — the row exists but ``thumbnail_path`` is
          NULL or the file is gone from disk (retention pruned it,
          private vault encrypted it, etc.); nothing was written.
        * ``"ocr_failed"`` — Tesseract raised or the binary is not
          configured; nothing was written.
        * ``"shot_not_found"`` — no row with that id.

    ``char_count_before`` / ``char_count_after`` are always present so
    the JSON shape stays stable; on a non-ok status both are ``0``.
    """

    status: RerunStatus
    shot_id: int
    char_count_before: int
    char_count_after: int


def _extract_sync(path: Path, langs: str, tesseract_path: Path | None) -> str:
    """Open the thumbnail and call Tesseract synchronously.

    Mirrors :func:`app.workers.ocr_worker._extract` so the inline re-run
    sees exactly the same pixel pipeline as the background worker — same
    PIL ``load()`` semantics, same lang string, same binary lookup.
    """
    with Image.open(path) as image:
        image.load()
        return extract_text(image, langs=langs, tesseract_path=tesseract_path)


async def rerun_ocr_for_shot(shot_id: int) -> RerunResult:
    """Re-run OCR for a single screenshot and persist the new text.

    All DB I/O uses parametrised SQL via :func:`app.storage.db.get_connection`.
    Tesseract is invoked through :func:`asyncio.to_thread`-equivalent only
    inside :mod:`app.workers.ocr_worker`; here we keep the call sync because
    the route runs in FastAPI's threadpool already (the OCR latency on a
    single thumbnail is well below the worker's batch budget).

    The function never raises on a missing image or an OCR failure — those
    are normal outcomes and surface as ``status`` values so the route can
    return ``200`` with a structured payload instead of a 500.
    """
    import asyncio  # noqa: PLC0415 — local import keeps module import-cheap

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, thumbnail_path, ocr_text, ocr_rerun_count "
            "FROM screenshots WHERE id = ?",
            (shot_id,),
        )
        row = await cursor.fetchone()

    if row is None:
        log.info("ocr_rerun.shot_not_found", shot_id=shot_id)
        return RerunResult(
            status="shot_not_found",
            shot_id=shot_id,
            char_count_before=0,
            char_count_after=0,
        )

    thumbnail_path_raw = row["thumbnail_path"]
    prev_text = row["ocr_text"]
    char_count_before = 0 if prev_text is None else len(str(prev_text))

    if thumbnail_path_raw is None:
        log.info("ocr_rerun.missing_thumbnail_column", shot_id=shot_id)
        return RerunResult(
            status="missing_image",
            shot_id=shot_id,
            char_count_before=char_count_before,
            char_count_after=char_count_before,
        )

    thumbnail_path = Path(str(thumbnail_path_raw))
    if not thumbnail_path.exists():
        log.info(
            "ocr_rerun.missing_thumbnail_file",
            shot_id=shot_id,
            thumbnail_path=str(thumbnail_path),
        )
        return RerunResult(
            status="missing_image",
            shot_id=shot_id,
            char_count_before=char_count_before,
            char_count_after=char_count_before,
        )

    settings = get_settings()

    try:
        raw_text = await asyncio.to_thread(
            _extract_sync,
            thumbnail_path,
            settings.tesseract_langs,
            settings.tesseract_path,
        )
    except OCRNotAvailable as exc:
        log.warning("ocr_rerun.unavailable", shot_id=shot_id, error=str(exc))
        return RerunResult(
            status="ocr_failed",
            shot_id=shot_id,
            char_count_before=char_count_before,
            char_count_after=char_count_before,
        )
    except Exception as exc:
        log.warning("ocr_rerun.extract_failed", shot_id=shot_id, error=str(exc))
        return RerunResult(
            status="ocr_failed",
            shot_id=shot_id,
            char_count_before=char_count_before,
            char_count_after=char_count_before,
        )

    # ``redact()`` returns ``None`` only when given a falsy input;
    # ``extract_text`` already returns a stripped ``str``. We coerce to
    # the empty string here so the downstream ``len()`` and the SQL
    # bind site both see a non-None value (``ocr_text`` is nullable in
    # the schema but the OCR worker convention is to write ``""``
    # rather than ``NULL`` for an empty result, and we follow it).
    redacted = redact(raw_text) or ""
    char_count_after = len(redacted)

    # v1.46 — append a snapshot of the PRIOR text to the per-shot OCR
    # revision log (migration 126). The very first re-run on a
    # never-rerun shot tags the prior snapshot as ``"initial"`` so the
    # UI can label it as "the original OCR pass". Subsequent re-runs
    # tag the prior snapshot as ``"rerun"`` (it was itself produced by
    # a previous re-run). Best-effort: any failure here must NOT break
    # the rerun, because the screenshot UPDATE below is the operator-
    # visible outcome and the revision log is forensic side-channel.
    #
    # ``ocr_rerun_count`` reflects the count *before* this re-run (we
    # bump it in the UPDATE below), so ``0`` means "this is the first
    # re-run on a row whose ocr_text was written by the background
    # worker" — that prior text is the ``initial`` revision.
    from app.ocr_rerun_history import (  # noqa: PLC0415 — local import keeps cycles impossible
        record_ocr_revision,
    )

    rerun_count_before = int(row["ocr_rerun_count"] or 0)
    prior_source = "initial" if rerun_count_before == 0 else "rerun"
    try:
        await record_ocr_revision(
            shot_id,
            "" if prev_text is None else str(prev_text),
            run_source=prior_source,
        )
    except Exception as exc:
        log.warning(
            "ocr_rerun.history_prior_failed",
            shot_id=shot_id,
            error=str(exc),
        )

    async with get_connection() as conn:
        await conn.execute(
            "UPDATE screenshots "
            "SET ocr_text = ?, "
            "    ocr_status = 'done', "
            "    ocr_rerun_count = COALESCE(ocr_rerun_count, 0) + 1 "
            "WHERE id = ?",
            (redacted, shot_id),
        )
        await conn.commit()

    # Mirror snapshot of the NEW text post-UPDATE. Same best-effort
    # rule: a failure here is a missed forensic row, not a broken
    # rerun. Always tagged ``"rerun"`` because by definition this row
    # is the output of an explicit manual re-run.
    try:
        await record_ocr_revision(shot_id, redacted, run_source="rerun")
    except Exception as exc:
        log.warning(
            "ocr_rerun.history_new_failed",
            shot_id=shot_id,
            error=str(exc),
        )

    log.info(
        "ocr_rerun.ok",
        shot_id=shot_id,
        char_count_before=char_count_before,
        char_count_after=char_count_after,
    )
    return RerunResult(
        status="ok",
        shot_id=shot_id,
        char_count_before=char_count_before,
        char_count_after=char_count_after,
    )
