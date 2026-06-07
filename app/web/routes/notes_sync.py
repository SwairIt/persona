"""Standalone-note write API that also emits sync_event rows.

The existing ``/api/notes`` GET handler lives in ``notes.py`` and is
left untouched. This module adds the WRITE side that was missing and
plumbs every mutation through the sync event log so the same change
fans out to the user's other devices.

Every mutation appends a ``note`` event with the new full payload —
``apply_pending`` materialises it back via UPSERT on ``notes.uuid``.
Because the local write also goes straight to the canonical table, we
do NOT need ``apply_pending`` to re-run for the same device that
produced the event (its applied_at gets stamped by the worker the next
time it picks up the row).
"""

from __future__ import annotations

import uuid as uuid_module
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.sync import append_event

router = APIRouter(tags=["notes-sync"])
log = get_logger("persona.notes_sync")

_MAX_BODY_BYTES = 64 * 1024
_MAX_TITLE_LEN = 200
_MAX_SOURCE_LEN = 64


def _coerce_payload(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="body must be an object")
    body = str(raw.get("body") or "")
    if not body:
        raise HTTPException(status_code=400, detail="body is required")
    if len(body.encode("utf-8")) > _MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body too large")
    title = raw.get("title")
    if title is not None and not isinstance(title, str):
        raise HTTPException(status_code=400, detail="title must be a string")
    if title is not None and len(title) > _MAX_TITLE_LEN:
        title = title[:_MAX_TITLE_LEN]
    source = str(raw.get("source") or "web")[:_MAX_SOURCE_LEN]
    return {"body": body, "title": title, "source": source}


@router.post("/api/notes", response_class=JSONResponse)
async def create_note(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """Create a standalone note. Returns the new uuid + numeric id."""
    payload = _coerce_payload(body)
    note_uuid = str(uuid_module.uuid4())
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO notes (uuid, title, body, source, created_at, updated_at, encrypted)
            VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), 0)
            """,
            (note_uuid, payload["title"], payload["body"], payload["source"]),
        )
        await conn.commit()
        new_id = cursor.lastrowid or 0

    # Fan-out to sync. We DO NOT block the user response on this; if the
    # event-log write fails we still keep the canonical note.
    try:
        await append_event(
            user_id=session["user_id"],
            kind="note",
            op="insert",
            entity_id=new_id,
            payload={
                "uuid": note_uuid,
                "title": payload["title"],
                "body": payload["body"],
                "source": payload["source"],
            },
        )
    except Exception as exc:
        log.warning("notes_sync.event_emit_failed", note_id=new_id, error=str(exc))

    log.info("notes_sync.created", note_id=new_id, uuid=note_uuid)
    return JSONResponse(
        {
            "id": new_id,
            "uuid": note_uuid,
            "title": payload["title"],
            "body": payload["body"],
            "source": payload["source"],
        },
        status_code=201,
    )


@router.patch("/api/notes/by-uuid/{note_uuid}", response_class=JSONResponse)
async def update_note(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    note_uuid: str,
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """Update a note by uuid + emit a sync event."""
    payload = _coerce_payload(body)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM notes WHERE uuid = ? AND deleted_at IS NULL",
            (note_uuid,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="note not found")
        note_id = int(row["id"])
        await conn.execute(
            """
            UPDATE notes SET title = ?, body = ?, source = ?,
                             updated_at = datetime('now')
             WHERE uuid = ?
            """,
            (payload["title"], payload["body"], payload["source"], note_uuid),
        )
        await conn.commit()

    try:
        await append_event(
            user_id=session["user_id"],
            kind="note",
            op="update",
            entity_id=note_id,
            payload={
                "uuid": note_uuid,
                "title": payload["title"],
                "body": payload["body"],
                "source": payload["source"],
            },
        )
    except Exception as exc:
        log.warning("notes_sync.event_emit_failed", note_id=note_id, error=str(exc))

    return JSONResponse({"id": note_id, "uuid": note_uuid, **payload})


@router.delete("/api/notes/by-uuid/{note_uuid}", response_class=JSONResponse)
async def delete_note(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    note_uuid: str,
) -> JSONResponse:
    """Soft-delete a note by uuid + emit a sync event."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM notes WHERE uuid = ? AND deleted_at IS NULL",
            (note_uuid,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="note not found")
        note_id = int(row["id"])
        await conn.execute(
            "UPDATE notes SET deleted_at = datetime('now') WHERE uuid = ?",
            (note_uuid,),
        )
        await conn.commit()

    try:
        await append_event(
            user_id=session["user_id"],
            kind="note",
            op="delete",
            entity_id=note_id,
            payload={"uuid": note_uuid},
        )
    except Exception as exc:
        log.warning("notes_sync.event_emit_failed", note_id=note_id, error=str(exc))

    return JSONResponse({"ok": True, "uuid": note_uuid})
