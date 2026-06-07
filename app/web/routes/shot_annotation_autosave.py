"""HTTP surface for screenshot annotation autosave + revisions (v1.22).

Three JSON endpoints layered on top of :mod:`app.shot_annotations` and
:mod:`app.shot_annotation_history`:

- ``POST /api/screenshot/{shot_id}/annotation/autosave`` — debounced
  2-second client-side fetch fires here while the user is still
  drawing. Both updates the live ``shot_annotation`` row (so a fresh
  page-load reflects the most recent state) and appends an immutable
  revision row tagged ``source='autosave'``. Returns the new
  ``revision_id`` + DB-side timestamp so the editor can show a
  "saved at HH:MM:SS" indicator without a follow-up GET.
- ``GET /api/screenshot/{shot_id}/annotation/revisions.json`` — list
  newest-first revisions for the timeline UI (autosave + manual
  interleaved by ``saved_at``).
- ``POST /api/screenshot/{shot_id}/annotation/restore/{revision_id}``
  — overwrite the live annotation with a prior revision's payload AND
  record a fresh ``source='manual'`` revision (so the restore itself
  is reversible by picking the row directly above it).

Architectural note: this router lives in its own file so the surgical
v1.22 addition does not bloat ``shot_annotations.py``. Registration
happens at app startup via the project's standard router-include
convention; this module is intentionally self-contained and is not
imported by :mod:`app.web.main` from this code path.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.logging_setup import get_logger
from app.shot_annotation_history import (
    get_revision,
    list_revisions,
    record_revision,
)
from app.shot_annotations import MAX_PAYLOAD_BYTES, upsert_annotation
from app.storage.db import get_connection
from app.storage.repository import get_screenshot

router = APIRouter(tags=["shot-annotation-autosave"])
log = get_logger("persona.web.shot_annotation_autosave")


class _AutosavePayload(BaseModel):
    """POST body for the autosave endpoint.

    ``max_length`` is in characters — a generous 4x cap rejects
    obviously-huge bodies before they reach the byte-precise check
    inside :func:`app.shot_annotations.upsert_annotation`.
    """

    svg_payload: str = Field(..., max_length=MAX_PAYLOAD_BYTES * 4)


async def _require_screenshot(shot_id: int) -> None:
    """Raise 404 if ``shot_id`` does not exist."""
    async with get_connection() as conn:
        shot = await get_screenshot(conn, shot_id)
    if shot is None:
        raise HTTPException(status_code=404, detail="Screenshot not found")


@router.post("/api/screenshot/{shot_id}/annotation/autosave")
async def annotation_autosave(
    request: Request,
    shot_id: int,
    payload: _AutosavePayload,
) -> JSONResponse:
    """Persist a debounced autosave for ``shot_id``.

    Writes both the live ``shot_annotation`` row (so a fresh page-load
    reflects the most recent state) and an immutable revision row.
    Returns the new ``revision_id`` and the live-row ``updated_at`` so
    the client can render a "saved at HH:MM:SS" indicator without a
    follow-up GET.

    T6 (2026-06-07) — also emits an ``annotation`` sync event so the
    SVG payload follows the user to their other devices. Emission is
    best-effort: failure never blocks the local autosave.
    """
    await _require_screenshot(shot_id)
    try:
        live = await upsert_annotation(shot_id, payload.svg_payload)
    except ValueError as exc:
        log.warning(
            "shot_annotation_autosave.too_large",
            shot_id=shot_id,
            error=str(exc),
        )
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    try:
        revision_id = await record_revision(
            shot_id, payload.svg_payload, source="autosave"
        )
    except ValueError as exc:
        # The size check inside ``record_revision`` mirrors the one we
        # just survived above, so reaching this branch means a stricter
        # future check was added there — fail loudly rather than silently
        # discarding the revision row.
        log.error(
            "shot_annotation_autosave.revision_rejected",
            shot_id=shot_id,
            error=str(exc),
        )
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    # T6 sync fan-out.
    from app.auth import current_user_optional  # noqa: PLC0415
    from app.shots import ensure_uuid  # noqa: PLC0415
    from app.sync import append_event  # noqa: PLC0415

    session = await current_user_optional(request)
    if session is not None:
        try:
            shot_uuid = await ensure_uuid(shot_id)
            if shot_uuid is not None:
                await append_event(
                    user_id=session["user_id"],
                    kind="annotation",
                    op="update",
                    payload={
                        "shot_uuid": shot_uuid,
                        "svg_payload": payload.svg_payload,
                    },
                )
        except Exception as exc:
            log.warning(
                "shot_annotation_autosave.event_emit_failed",
                shot_id=shot_id,
                error=str(exc),
            )

    return JSONResponse(
        {
            "revision_id": revision_id,
            "updated_at": live["updated_at"],
            "source": "autosave",
        }
    )


@router.get("/api/screenshot/{shot_id}/annotation/revisions.json")
async def annotation_revisions_list(shot_id: int) -> JSONResponse:
    """Return recent revisions for ``shot_id`` (newest first).

    Hard-capped at :data:`app.shot_annotation_history.MAX_AUTOSAVES_PER_SHOT`
    by the helper. The ``svg_payload`` column is intentionally omitted
    from the listing response — the timeline UI only needs id / source
    / timestamp; clients fetch the full payload via /restore when the
    user actually picks a row.
    """
    await _require_screenshot(shot_id)
    rows = await list_revisions(shot_id)
    return JSONResponse(
        {
            "shot_id": shot_id,
            "revisions": [
                {
                    "id": r["id"],
                    "saved_at": r["saved_at"],
                    "source": r["source"],
                    "bytes": len(r["svg_payload"].encode("utf-8")),
                }
                for r in rows
            ],
        }
    )


@router.post("/api/screenshot/{shot_id}/annotation/restore/{revision_id}")
async def annotation_restore(
    shot_id: int,
    revision_id: int,
) -> JSONResponse:
    """Revert the live annotation to ``revision_id``'s payload.

    Records the restore itself as a fresh ``source='manual'`` revision
    so the user can undo the undo by clicking the row directly above
    the one they just restored.
    """
    await _require_screenshot(shot_id)
    revision = await get_revision(revision_id)
    if revision is None:
        raise HTTPException(status_code=404, detail="Revision not found")
    if revision["screenshot_id"] != shot_id:
        # Belt-and-braces: a revision id from another shot must not be
        # restorable onto this one, even if the caller fabricated the
        # URL. The 404 (not 403) keeps the existence of the foreign
        # revision invisible to the caller.
        raise HTTPException(status_code=404, detail="Revision not found")

    payload = revision["svg_payload"]
    try:
        live = await upsert_annotation(shot_id, payload)
    except ValueError as exc:
        log.error(
            "shot_annotation_autosave.restore_too_large",
            shot_id=shot_id,
            revision_id=revision_id,
            error=str(exc),
        )
        raise HTTPException(status_code=413, detail=str(exc)) from exc

    new_revision_id = await record_revision(
        shot_id, payload, source="manual"
    )
    log.info(
        "shot_annotation_autosave.restore",
        shot_id=shot_id,
        from_revision=revision_id,
        new_revision=new_revision_id,
    )
    return JSONResponse(
        {
            "shot_id": shot_id,
            "restored_from": revision_id,
            "new_revision_id": new_revision_id,
            "updated_at": live["updated_at"],
            "source": "manual",
        }
    )
