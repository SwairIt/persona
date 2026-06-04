"""HTTP endpoint that accepts a browser ``MediaRecorder`` upload.

Single route: ``POST /api/voice-note/upload``. The companion widget
(:mod:`app.web.routes.voice_note_widget`) renders the record button and
the JavaScript that POSTs the resulting blob here.

Status-code contract
--------------------
The route maps the dict returned by
:func:`app.voice_note.ingest_voice_note` to the response shape the
browser fragment expects:

* ``201`` on success — body is the full result dict (so the widget can
  link straight to the inserted note via ``note_id``).
* ``413`` when the uploaded blob exceeds the 5 MiB cap.
* ``415`` when ``Content-Type`` is neither ``audio/...`` nor absent.
* ``503`` when no Whisper backend is installed — the audio file was
  still written, but the transcript could not be produced. The widget
  shows the operator a hint to install ``openai-whisper`` /
  ``faster-whisper``.
* ``500`` when a Whisper backend is installed but the transcription
  itself raised. The audio file is preserved on disk.
* ``400`` for an empty upload.

The 5 MiB cap matches the existing
:mod:`app.web.routes.notes_csv_import` route and is enforced *after*
``await file.read()`` — Starlette doesn't stream-truncate uploads and a
5 MiB allocation per request is acceptable for an explicit operator
action.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main` — the task spec forbids touching ``main.py``.
Wire it up with::

    from app.web.routes import voice_note as voice_note_routes
    app.include_router(voice_note_routes.router)
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger
from app.voice_note import ingest_voice_note

router = APIRouter(tags=["voice-note"])

log = get_logger("persona.voice_note.routes")

# Hard cap on the uploaded blob — matches the spec and the existing
# notes-CSV-import cap. 5 MiB ~= 5 minutes of opus at 128 kbps, which
# covers the typical "quick voice memo" use case with comfortable
# headroom.
_MAX_UPLOAD_BYTES: Final[int] = 5 * 1024 * 1024

# MIME-type guard. The browser sends whatever ``MediaRecorder.mimeType``
# resolved to (typically ``audio/webm;codecs=opus`` on Chromium or
# ``audio/ogg;codecs=opus`` on Firefox). We accept anything that starts
# with ``audio/`` so we don't have to chase the codec suffix matrix
# across UA versions; non-audio MIME — or a request without one at all —
# answers ``415``.
_AUDIO_MIME_PREFIX: Final[str] = "audio/"


def _is_audio_mime(content_type: str | None) -> bool:
    """Return ``True`` for any ``audio/...`` content type.

    Tolerates the ``;codecs=...`` suffix by checking only the prefix.
    Empty / ``None`` MIME is treated as *not* audio so the route can
    reject ``application/octet-stream`` uploads that bypass the widget.
    """
    if not content_type:
        return False
    return content_type.strip().lower().startswith(_AUDIO_MIME_PREFIX)


@router.post("/api/voice-note/upload")
async def upload_voice_note(
    request: Request,
    file: Annotated[UploadFile, File(...)],
) -> JSONResponse:
    """Accept a multipart voice-note upload, transcribe, insert a note.

    The route is intentionally a thin HTTP shell: all the business logic
    (path derivation, file write, transcription, DB insert) lives in
    :func:`app.voice_note.ingest_voice_note`. The mapping of the
    ingester's ``status`` to an HTTP status code is the only
    HTTP-specific decision the route owns.
    """
    client = request.client.host if request.client is not None else None

    mime = file.content_type or ""
    if not _is_audio_mime(mime):
        log.warning(
            "voice_note.upload.bad_mime",
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
        log.warning("voice_note.upload.empty", client=client)
        raise HTTPException(status_code=400, detail="empty upload")
    if size > _MAX_UPLOAD_BYTES:
        log.warning(
            "voice_note.upload.too_large",
            client=client,
            bytes=size,
            limit=_MAX_UPLOAD_BYTES,
        )
        raise HTTPException(status_code=413, detail="upload too large")

    result = await ingest_voice_note(raw, mime)
    status = result.get("status")

    if status == "missing_dep":
        log.warning(
            "voice_note.upload.missing_backend",
            client=client,
            bytes=size,
        )
        # 503 — "service unavailable" matches the semantics: the upload
        # is fine, the *transcription* dependency is missing on the
        # server side. The audio file was still saved (path is in the
        # body) so the operator can re-run after installing a backend.
        return JSONResponse(status_code=503, content=result)

    if status == "transcribe_failed":
        log.warning(
            "voice_note.upload.transcribe_failed",
            client=client,
            bytes=size,
        )
        return JSONResponse(status_code=500, content=result)

    log.info(
        "voice_note.upload.ok",
        client=client,
        bytes=size,
        note_id=result.get("note_id"),
    )
    return JSONResponse(status_code=201, content=result)


__all__ = ["router"]
