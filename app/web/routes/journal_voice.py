"""HTTP endpoints for voice dictation into the journal.

Two routes:

* ``POST /api/journal/voice-dictate`` — accept a ``MediaRecorder`` blob,
  transcribe, insert a ``notes`` row tagged ``source = 'journal_voice'``,
  return the ingest result as JSON.
* ``GET /journal/voice`` — render a standalone dictation page (big
  record button + live transcript preview).

Status-code contract for the upload route mirrors the v1.36 voice-note
upload route so the front-end can reuse the same status-handling
pattern:

* ``201`` — happy path; body is the full ingest dict (so the page can
  link to the new note via ``note_id``).
* ``413`` — upload exceeded the 5 MiB cap.
* ``415`` — ``Content-Type`` was not ``audio/...``.
* ``503`` — no Whisper backend installed (audio file was still written).
* ``500`` — backend present but transcription raised.
* ``400`` — empty upload.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main` — the task spec forbids touching ``main.py``.
Wire it up with::

    from app.web.routes import journal_voice as journal_voice_routes
    app.include_router(journal_voice_routes.router)
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from app.journal_voice import ingest_journal_dictation
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["journal-voice"])

log = get_logger("persona.journal_voice.routes")

# Hard cap on the upload — matches the v1.36 voice-note route. 5 MiB
# is roughly 5 minutes of opus at 128 kbps, which covers the typical
# dictation use case with comfortable headroom.
_MAX_UPLOAD_BYTES: Final[int] = 5 * 1024 * 1024

# MIME guard. ``MediaRecorder`` emits ``audio/webm;codecs=opus`` on
# Chromium, ``audio/ogg;codecs=opus`` on Firefox, ``audio/mp4`` on
# Safari. Accepting any ``audio/...`` prefix avoids chasing the codec
# matrix across UA versions.
_AUDIO_MIME_PREFIX: Final[str] = "audio/"


def _is_audio_mime(content_type: str | None) -> bool:
    """``True`` for any ``audio/...`` content type (tolerates ``;codecs=…``)."""
    if not content_type:
        return False
    return content_type.strip().lower().startswith(_AUDIO_MIME_PREFIX)


@router.post("/api/journal/voice-dictate")
async def dictate_into_journal(
    request: Request,
    file: Annotated[UploadFile, File(...)],
) -> JSONResponse:
    """Accept a dictation upload, transcribe, append a journal note.

    Intentionally a thin HTTP shell: all business logic lives in
    :func:`app.journal_voice.ingest_journal_dictation`. The only
    HTTP-specific decision this route owns is mapping the ingester's
    ``status`` to an HTTP status code.
    """
    client = request.client.host if request.client is not None else None

    mime = file.content_type or ""
    if not _is_audio_mime(mime):
        log.warning(
            "journal_voice.upload.bad_mime",
            client=client,
            content_type=mime,
        )
        raise HTTPException(
            status_code=415,
            detail="unsupported media type — expected audio/*",
        )

    raw = await file.read()
    size = len(raw)

    if size == 0:
        log.warning("journal_voice.upload.empty", client=client)
        raise HTTPException(status_code=400, detail="empty upload")
    if size > _MAX_UPLOAD_BYTES:
        log.warning(
            "journal_voice.upload.too_large",
            client=client,
            bytes=size,
            limit=_MAX_UPLOAD_BYTES,
        )
        raise HTTPException(status_code=413, detail="upload too large")

    result = await ingest_journal_dictation(raw, mime)
    status = result.get("status")

    if status == "missing_dep":
        log.warning(
            "journal_voice.upload.missing_backend",
            client=client,
            bytes=size,
        )
        # 503 — service unavailable; the dictation upload itself is
        # fine, the *transcription* dependency is missing on the server.
        return JSONResponse(status_code=503, content=result)

    if status == "transcribe_failed":
        log.warning(
            "journal_voice.upload.transcribe_failed",
            client=client,
            bytes=size,
        )
        return JSONResponse(status_code=500, content=result)

    log.info(
        "journal_voice.upload.ok",
        client=client,
        bytes=size,
        note_id=result.get("note_id"),
        chars=result.get("char_count"),
    )
    return JSONResponse(status_code=201, content=result)


@router.get("/journal/voice", response_class=HTMLResponse)
async def journal_voice_page(request: Request) -> HTMLResponse:
    """Render the standalone dictation page.

    Pure server-side template render — no DB hit on GET. The page
    itself drives ``MediaRecorder`` client-side and POSTs to
    ``/api/journal/voice-dictate``.
    """
    return templates.TemplateResponse(
        request,
        "journal_voice.html",
        {
            "title": "Голосовая запись",
            "active_nav": "journal",
        },
    )


__all__ = ["router"]
