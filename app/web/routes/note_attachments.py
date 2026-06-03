"""Audio attachments for standalone notes (v1.1 feature 2/3).

The standalone ``notes`` table (``039_inbox_notes.sql``) only ever
stored a textual body. This module adds the missing capability: pin one
or more *audio* files onto a note — a voice memo dropped from a phone,
a quick dictation recorded in the browser, or an OGG file dragged out
of another app. The textual note is still the authoritative record;
the audio sits next to it as auxiliary context.

Surface
-------

* ``POST /api/notes/{note_id}/attach``
    Multipart ``file`` upload. Validates that the parent note exists,
    rejects anything bigger than :data:`_MAX_UPLOAD_BYTES` (25 MiB) or
    with a MIME type that does not start with ``audio/``, writes the
    bytes to disk under :file:`<data_dir>/note_attachments/`, and
    inserts a fresh row into ``note_attachment``.

* ``GET /api/note-attachment/{att_id}/file``
    Streams the raw audio bytes back to the caller, echoing the stored
    MIME as ``Content-Type``. The on-disk path is resolved against
    ``settings.data_dir`` and a directory-traversal check is enforced
    so a malicious ``path`` column (impossible in practice — we write
    it ourselves — but defence in depth) cannot read arbitrary files.

* ``GET /api/notes/{note_id}/attachments.json``
    Machine-readable listing for the HTML page that renders the audio
    players. Encrypted parent notes are *not* a special case here: the
    audio is its own data and remains accessible. Locking the audio
    behind the note's encryption is left as a follow-up.

* ``POST /api/note-attachment/{att_id}/delete``
    Deletes the row and unlinks the file. ``POST`` (not ``DELETE``)
    keeps the form-driven HTML page working without needing an XHR
    interceptor — every other "delete" endpoint in this codebase uses
    the same convention.

Storage layout
--------------

::

    <data_dir>/
        note_attachments/
            <id>-<sanitised-filename>.<ext>

The row id is baked into the on-disk filename so concurrent uploads of
two files with the same source name cannot collide. The ``path``
column stores the path *relative* to ``data_dir`` so the database is
portable across machines.

Validation layers
-----------------

The MIME check is intentionally layered, fastest first:

1. Empty / too-large rejected before anything else.
2. ``Content-Type`` must start with ``audio/`` (any subtype — we don't
   gate on a specific codec because the browser will refuse anything
   the user's UA can't decode, and we'd rather not encode a stale list
   of codecs here).
3. The filename is sanitised (path-traversal, control characters) and
   bounded to :data:`_MAX_FILENAME_LEN`.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main` — the task spec forbids touching ``main.py``.
Wire it up with::

    from app.web.routes import note_attachments as note_attachments_routes
    app.include_router(note_attachments_routes.router)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Final

import anyio
from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection

log = get_logger("persona.note_attachments")

router = APIRouter(tags=["note-attachments"])

# 25 MiB hard ceiling on a single audio file. Long enough for a ~25 min
# 128 kbps MP3 voice memo or a much longer Opus file; short enough to
# stay out of "this should be a podcast hosting service" territory.
_MAX_UPLOAD_BYTES: Final[int] = 25 * 1024 * 1024

# Cap on the operator-supplied filename we preserve on disk. Keeps the
# slug short and avoids OS-level path-length headaches on Windows. The
# on-disk file is also prefixed with the attachment row id so two
# uploads sharing a source filename cannot collide.
_MAX_FILENAME_LEN: Final[int] = 80

# Drop everything outside this set so a hostile filename like
# ``../../etc/passwd`` collapses to ``etcpasswd`` before we ever hand
# it to ``Path``. The dot is kept so the extension survives.
_SAFE_FILENAME_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9._-]+")

# Sub-folder name under ``settings.data_dir`` where the actual audio
# bytes live. Matches the ``manual/`` / ``thumbnails/`` convention used
# elsewhere in the codebase — operator-visible, sortable in a file
# manager, and easy to grep for.
_STORAGE_SUBDIR: Final[str] = "note_attachments"

# Default filename for an upload whose name we cannot rescue — only
# ever triggered when every byte was scrubbed by ``_SAFE_FILENAME_RE``.
_FALLBACK_STEM: Final[str] = "audio"
_FALLBACK_EXT: Final[str] = "bin"

# MIME types we recognise so the streaming endpoint can hand the
# browser a sensible default extension when it has to fabricate one. We
# accept ANY ``audio/*`` upload — this map only drives the *fallback*
# filename path, never validation.
_MIME_TO_EXT: Final[dict[str, str]] = {
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/x-wav": "wav",
    "audio/ogg": "ogg",
    "audio/opus": "opus",
    "audio/webm": "webm",
    "audio/aac": "aac",
    "audio/flac": "flac",
    "audio/mp4": "m4a",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _storage_dir() -> Path:
    """Resolve and ensure :file:`<data_dir>/note_attachments/` exists.

    Created lazily (not at import time) because :func:`get_settings`
    resolves the data root from the environment — unit tests that
    override ``PERSONA_DATA_DIR`` would otherwise see a folder
    materialised under the wrong path at import.
    """
    settings = get_settings()
    out = settings.data_dir / _STORAGE_SUBDIR
    out.mkdir(parents=True, exist_ok=True)
    return out


def _ext_for_mime(mime: str) -> str:
    """Return a best-guess file extension for ``mime``.

    Used only to fabricate a filename for uploads whose ``filename``
    field is missing or fully scrubbed by the safe-name regex. ``mime``
    has already been validated to start with ``audio/``.
    """
    lowered = mime.lower()
    return _MIME_TO_EXT.get(lowered, _FALLBACK_EXT)


def _sanitise_filename(name: str | None, fallback_ext: str) -> str:
    """Return a filesystem-safe, length-bounded filename.

    Strips path separators, normalises odd characters, and guarantees
    an extension. Never produces a name starting with ``.`` (so it
    cannot accidentally collide with a hidden file) or longer than
    :data:`_MAX_FILENAME_LEN`.
    """
    raw_name = (name or "").strip()
    # ``Path(raw_name).name`` strips any path component the browser may
    # have leaked, so ``"../../etc/passwd.mp3"`` collapses to
    # ``"passwd.mp3"`` before the regex scrub runs.
    base = Path(raw_name).name
    cleaned = _SAFE_FILENAME_RE.sub("_", base).strip("._")
    if not cleaned:
        cleaned = f"{_FALLBACK_STEM}.{fallback_ext}"
    elif "." not in cleaned:
        cleaned = f"{cleaned}.{fallback_ext}"
    if len(cleaned) > _MAX_FILENAME_LEN:
        # Preserve the extension when truncating the head.
        stem, dot, ext = cleaned.rpartition(".")
        if dot and ext:
            keep = _MAX_FILENAME_LEN - len(ext) - 1
            cleaned = f"{stem[: max(1, keep)]}.{ext}"
        else:
            cleaned = cleaned[:_MAX_FILENAME_LEN]
    return cleaned


def _validate_mime(declared_mime: str | None) -> str:
    """Return the validated MIME or raise 400.

    Accepts any ``audio/<subtype>`` value. We deliberately do *not*
    gate on a specific subtype because the browser is the authoritative
    decoder — the user knows whether their UA plays a given file. We
    also reject an empty / missing ``Content-Type`` outright; sending
    binary blobs with no declared type is almost always a
    misconfigured client.
    """
    if not declared_mime:
        raise HTTPException(status_code=400, detail="missing content type")
    lowered = declared_mime.lower().strip()
    if not lowered.startswith("audio/"):
        raise HTTPException(
            status_code=400,
            detail=f"unsupported content type: {declared_mime}",
        )
    # Strip ``;charset=...`` and the like — irrelevant for binary.
    return lowered.split(";", 1)[0].strip()


def _write_bytes_sync(target: Path, raw: bytes) -> None:
    """Sync helper called via ``anyio.to_thread`` to avoid blocking the loop."""
    target.write_bytes(raw)


def _unlink_sync(target: Path) -> None:
    """Sync helper called via ``anyio.to_thread`` to avoid blocking the loop.

    ``missing_ok=True`` so a stale row whose file was already cleaned
    up out-of-band (manual rm, disk wipe, ...) still deletes cleanly.
    """
    target.unlink(missing_ok=True)


def _resolve_attachment_path(stored_path: str) -> Path:
    """Resolve ``stored_path`` against ``data_dir`` and verify containment.

    ``stored_path`` is whatever the database holds (we always write a
    relative path, but a defence-in-depth check guarantees we never
    serve a file outside the data directory even if the row was
    tampered with).
    """
    settings = get_settings()
    candidate = (settings.data_dir / stored_path).resolve()
    data_root = settings.data_dir.resolve()
    try:
        candidate.relative_to(data_root)
    except ValueError as exc:
        log.warning(
            "note_attachments.path_escape_blocked",
            stored_path=stored_path,
            resolved=str(candidate),
        )
        raise HTTPException(status_code=404, detail="not found") from exc
    return candidate


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Project an ``aiosqlite.Row`` from ``note_attachment`` to public JSON.

    The ``path`` column is intentionally **not** surfaced — clients
    should fetch the bytes via the streaming endpoint, not poke at the
    raw filesystem path. Exposing it would also leak the configured
    ``data_dir`` to the caller.
    """
    return {
        "id": int(row["id"]),
        "note_id": int(row["note_id"]),
        "filename": str(row["filename"]),
        "mime": str(row["mime"]),
        "size_bytes": int(row["size_bytes"]),
        "created_at": str(row["created_at"]),
        "file_url": f"/api/note-attachment/{int(row['id'])}/file",
    }


async def _note_exists(note_id: int) -> bool:
    """Return ``True`` iff a row with ``id == note_id`` exists in ``notes``.

    The upload endpoint refuses to attach an audio file to a phantom
    note id — without this check a typo'd ``note_id`` would silently
    create an orphan row (FK enforcement only triggers on
    ``ON DELETE CASCADE``, not on the original INSERT, because SQLite
    allows the insert through and only enforces on a future delete of
    the missing parent — defensive programming, not paranoia).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM notes WHERE id = ?",
            (int(note_id),),
        )
        row = await cursor.fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/api/notes/{note_id}/attach")
async def attach_audio(
    note_id: int,
    file: Annotated[UploadFile, File(...)],
) -> JSONResponse:
    """Attach an audio file to a standalone note.

    Pipeline:

    1. Verify the parent note exists.
    2. Read the upload, enforce the byte ceiling and the
       ``audio/*`` MIME constraint.
    3. Sanitise the filename.
    4. ``INSERT INTO note_attachment`` so we have an id to embed in the
       on-disk filename.
    5. Persist the bytes under
       :file:`<data_dir>/note_attachments/<id>-<safe-name>`.
    6. ``UPDATE note_attachment SET path = ?`` so the row reflects what
       actually landed on disk.
    """
    if not await _note_exists(note_id):
        raise HTTPException(status_code=404, detail="note not found")

    declared_mime = (file.content_type or None) if file is not None else None
    mime = _validate_mime(declared_mime)

    raw = await file.read()
    if len(raw) == 0:
        raise HTTPException(status_code=400, detail="empty upload")
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="upload too large")

    fallback_ext = _ext_for_mime(mime)
    safe_name = _sanitise_filename(file.filename, fallback_ext)

    # Insert the row first to get a stable id; we write the path back in
    # a second statement once the file is on disk so a crash mid-upload
    # leaves an obviously-broken row (``path == ''``) that an admin can
    # spot, rather than a row claiming a file that never materialised.
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO note_attachment (note_id, filename, mime, size_bytes, path)
            VALUES (?, ?, ?, ?, '')
            """,
            (int(note_id), safe_name, mime, len(raw)),
        )
        att_id_raw = cursor.lastrowid
        if att_id_raw is None:
            await conn.rollback()
            msg = "note_attachment insert returned no id"
            raise RuntimeError(msg)
        att_id = int(att_id_raw)
        await conn.commit()

    storage_dir = _storage_dir()
    on_disk_name = f"{att_id}-{safe_name}"
    target = storage_dir / on_disk_name
    relative_path = f"{_STORAGE_SUBDIR}/{on_disk_name}"

    await anyio.to_thread.run_sync(_write_bytes_sync, target, raw)

    async with get_connection() as conn:
        await conn.execute(
            "UPDATE note_attachment SET path = ? WHERE id = ?",
            (relative_path, att_id),
        )
        await conn.commit()

    log.info(
        "note_attachments.attach",
        note_id=note_id,
        attachment_id=att_id,
        filename=safe_name,
        mime=mime,
        size_bytes=len(raw),
        path=relative_path,
    )

    return JSONResponse(
        {
            "ok": True,
            "id": att_id,
            "note_id": note_id,
            "filename": safe_name,
            "mime": mime,
            "size_bytes": len(raw),
            "file_url": f"/api/note-attachment/{att_id}/file",
        },
        status_code=201,
    )


@router.get("/api/note-attachment/{att_id}/file")
async def stream_audio(att_id: int) -> FileResponse:
    """Stream the raw audio bytes for one attachment.

    The stored MIME is echoed back as ``Content-Type``. The original
    operator-visible filename is exposed via
    ``Content-Disposition: inline; filename="..."`` so a "Save As…"
    from the browser produces something the operator will recognise.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, filename, mime, path
              FROM note_attachment
             WHERE id = ?
            """,
            (int(att_id),),
        )
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")

    stored_path = str(row["path"])
    if not stored_path:
        # Upload that crashed between INSERT and the file write.
        log.warning("note_attachments.empty_path", attachment_id=att_id)
        raise HTTPException(status_code=404, detail="not found")

    resolved = _resolve_attachment_path(stored_path)
    if not resolved.exists():
        log.warning(
            "note_attachments.file_missing",
            attachment_id=att_id,
            path=stored_path,
        )
        raise HTTPException(status_code=404, detail="not found")

    log.info(
        "note_attachments.stream",
        attachment_id=att_id,
        mime=str(row["mime"]),
    )
    return FileResponse(
        path=resolved,
        media_type=str(row["mime"]),
        filename=str(row["filename"]),
        # ``inline`` so the browser's <audio> element can play it
        # straight away; ``filename`` only matters when the user picks
        # "Save As…" from the right-click menu.
        content_disposition_type="inline",
    )


@router.get("/api/notes/{note_id}/attachments.json")
async def list_attachments(note_id: int) -> JSONResponse:
    """List every audio attachment pinned to ``note_id``, newest first.

    Returns an empty ``items`` array (with HTTP 200) when the note has
    no attachments — easier on the front-end than a 404, and matches
    what every other list endpoint in this codebase does. Whether the
    parent note exists is *not* checked here on purpose: a stale UI
    asking about a since-deleted note's attachments will just get an
    empty list back, which is the desired behaviour.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, note_id, filename, mime, size_bytes, created_at
              FROM note_attachment
             WHERE note_id = ?
             ORDER BY id DESC
            """,
            (int(note_id),),
        )
        rows = await cursor.fetchall()

    items = [_row_to_dict(row) for row in rows]
    log.info(
        "note_attachments.list",
        note_id=note_id,
        count=len(items),
    )
    return JSONResponse({"note_id": note_id, "items": items, "total": len(items)})


@router.post("/api/note-attachment/{att_id}/delete")
async def delete_attachment(att_id: int) -> JSONResponse:
    """Delete one attachment row and unlink its on-disk file.

    The DB row is removed *after* the file is unlinked so a crash
    between the two steps leaves an orphan row (visible in the listing
    endpoint, easy to clean up) rather than a phantom file (invisible
    to the app, harder to find). The unlink uses ``missing_ok=True`` so
    a row whose file was already deleted out-of-band still cleans up.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, path FROM note_attachment WHERE id = ?",
            (int(att_id),),
        )
        row = await cursor.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="not found")

    stored_path = str(row["path"])
    if stored_path:
        try:
            resolved = _resolve_attachment_path(stored_path)
        except HTTPException:
            # The defence-in-depth path-escape check fired. We still
            # want the row gone so a manual cleanup can recover; log
            # and continue.
            log.warning(
                "note_attachments.delete_path_escape",
                attachment_id=att_id,
                path=stored_path,
            )
        else:
            await anyio.to_thread.run_sync(_unlink_sync, resolved)

    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM note_attachment WHERE id = ?",
            (int(att_id),),
        )
        await conn.commit()

    log.info("note_attachments.delete", attachment_id=att_id)
    return JSONResponse({"ok": True, "id": att_id, "deleted": True})


__all__ = ["router"]
