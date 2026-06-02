"""Notes attached to individual screenshots **and** standalone notes.

Two distinct backing tables coexist here, kept in one router so the
public URL prefix (``/api/screenshots/{id}/note`` for screenshot
attachments, ``/api/notes/{id}`` for standalone) stays grouped under a
single ``notes`` OpenAPI tag.

v0.45 adds two endpoints for the standalone ``notes`` table:

    * ``GET  /api/notes`` — list recent standalone notes. Encrypted rows
      come back with ``"body": ""``, ``"encrypted": true`` and a
      ``"marker": "[locked]"`` literal that callers (CLI / HTML) can
      surface verbatim. The plaintext body is **never** included in
      list responses, even for unlocked rows we know are decryptable.
    * ``POST /api/notes/{id}/view`` — return a single note. For an
      encrypted row this requires a form-posted ``password`` field and
      delegates decryption to :mod:`app.encrypted_notes`; plaintext rows
      ignore the password if supplied.

The bulk of the encryption surface (encrypt / decrypt / unlock-token
mint / list-encrypted) lives in :mod:`app.web.routes.encrypted_notes` —
this module only handles the "list + show" reading side so listings
naturally hide the ciphertext.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Query
from fastapi.responses import JSONResponse

from app.encrypted_notes import BadPassword, decrypt_note
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.notes import delete_note, upsert_note
from app.storage.repository import get_screenshot

log = get_logger("persona.notes.routes")

# Sentinel string that callers can render verbatim where they'd otherwise
# show a body preview. The task spec is explicit: "no emoji — use the
# literal text '[locked]'" so HTML / TUI clients pick this up without
# having to know about lock icons.
LOCKED_MARKER = "[locked]"

# Cap on /api/notes; keeps a misbehaving client from materialising the
# whole notes table into a single JSON response.
_MAX_LIST_LIMIT = 200

router = APIRouter(tags=["notes"])


# ---------------------------------------------------------------------------
# Screenshot-attached notes (unchanged from v0.44)
# ---------------------------------------------------------------------------


@router.post("/api/screenshots/{screenshot_id}/note", response_class=JSONResponse)
async def save_note(screenshot_id: int, body: str = Form(...)) -> JSONResponse:
    async with get_connection() as conn:
        existing = await get_screenshot(conn, screenshot_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="Screenshot not found")
        text = body.strip()
        if not text:
            await delete_note(conn, screenshot_id)
        else:
            await upsert_note(conn, screenshot_id, text)
    return JSONResponse({"screenshot_id": screenshot_id, "note": text})


@router.delete("/api/screenshots/{screenshot_id}/note", response_class=JSONResponse)
async def remove_note(screenshot_id: int) -> JSONResponse:
    async with get_connection() as conn:
        await delete_note(conn, screenshot_id)
    return JSONResponse({"screenshot_id": screenshot_id, "deleted": True})


# ---------------------------------------------------------------------------
# Standalone notes (v0.45 — encryption-aware list + detail)
# ---------------------------------------------------------------------------


def _project_list_row(row: Any) -> dict[str, Any]:
    """Build the public JSON shape for one row of /api/notes.

    Encrypted rows replace ``body`` with the empty string and add a
    ``marker`` field carrying the literal ``[locked]`` sentinel. We
    never include the ciphertext bytes; the binary blob has no place in
    a listing endpoint.
    """
    is_encrypted = bool(int(row["encrypted"] or 0))
    item: dict[str, Any] = {
        "id": int(row["id"]),
        "title": (str(row["title"]) if row["title"] is not None else None),
        "source": (str(row["source"]) if row["source"] is not None else None),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
        "encrypted": is_encrypted,
    }
    if is_encrypted:
        item["body"] = ""
        item["marker"] = LOCKED_MARKER
    else:
        item["body"] = str(row["body"])
    return item


@router.get("/api/notes", response_class=JSONResponse)
async def list_notes(
    limit: int = Query(default=50, ge=1, le=_MAX_LIST_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    """Return recent standalone notes, newest first.

    Encrypted rows are still listed (so the UI can show a "locked"
    marker), but the ``body`` field is forced to an empty string and a
    ``marker`` field carries the literal ``[locked]`` sentinel. The
    encryption-bearing ``ciphertext`` blob is never serialised.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, title, body, source, created_at, updated_at, encrypted
              FROM notes
             ORDER BY id DESC
             LIMIT ? OFFSET ?
            """,
            (int(limit), int(offset)),
        )
        rows = await cursor.fetchall()

    items = [_project_list_row(row) for row in rows]
    log.info("notes.list", count=len(items), limit=limit, offset=offset)
    return JSONResponse({"items": items, "total": len(items)})


@router.post("/api/notes/{note_id}/view", response_class=JSONResponse)
async def view_note(
    note_id: int,
    password: str = Form(default=""),
) -> JSONResponse:
    """Return a single note. Encrypted rows require ``password``.

    Plaintext rows are returned as-is and ignore any password value the
    caller supplied. Encrypted rows delegate to
    :func:`app.encrypted_notes.decrypt_note`, which also writes the
    audit-log entry for the attempt. The plaintext appears exactly
    once, in the response body — it is not re-persisted.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, title, body, source, created_at, updated_at, encrypted
              FROM notes
             WHERE id = ?
            """,
            (int(note_id),),
        )
        row = await cursor.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail={"status": "not_found"})

    is_encrypted = bool(int(row["encrypted"] or 0))
    if not is_encrypted:
        log.info("notes.view.plain", note_id=note_id)
        return JSONResponse(
            {
                "id": int(row["id"]),
                "title": (str(row["title"]) if row["title"] is not None else None),
                "body": str(row["body"]),
                "source": (str(row["source"]) if row["source"] is not None else None),
                "created_at": str(row["created_at"]),
                "updated_at": str(row["updated_at"]),
                "encrypted": False,
            }
        )

    if not password:
        # Surface the lock state without leaking anything else; the
        # client should pop up a password prompt.
        log.info("notes.view.locked", note_id=note_id)
        raise HTTPException(
            status_code=401,
            detail={
                "status": "password_required",
                "marker": LOCKED_MARKER,
                "note_id": int(note_id),
            },
        )

    try:
        plaintext = await decrypt_note(note_id, password)
    except BadPassword as exc:
        log.info("notes.view.bad_password", note_id=note_id)
        raise HTTPException(
            status_code=403,
            detail={"status": "bad_password", "error": str(exc)},
        ) from exc

    log.info("notes.view.unlocked", note_id=note_id)
    return JSONResponse(
        {
            "id": int(row["id"]),
            "title": (str(row["title"]) if row["title"] is not None else None),
            "body": plaintext,
            "source": (str(row["source"]) if row["source"] is not None else None),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "encrypted": True,
        }
    )
