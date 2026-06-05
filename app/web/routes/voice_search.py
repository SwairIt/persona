"""HTTP endpoints for the voice-search feature.

Two routes:

* ``GET  /search/voice`` — render the standalone Tailwind page with the
  prominent record button + live-results pane.
* ``POST /api/voice-search`` — accept a multipart audio upload, hand
  it to :func:`app.voice_search.transcribe_and_search`, and return the
  ``{status, transcript, results, total_matches}`` JSON shape verbatim.

Status-code contract
--------------------
The route maps the dict returned by
:func:`app.voice_search.transcribe_and_search` onto the HTTP layer:

* ``200`` on ``"ok"`` and ``"empty_transcript"`` — the request was
  processed fine; the UI inspects ``status`` for the silent-recording
  case. We do not 4xx ``"empty_transcript"`` because the upload itself
  was valid; the transcript just happens to be empty.
* ``413`` when the uploaded blob exceeds the 5 MiB cap (matches the
  existing :mod:`app.web.routes.voice_note` cap).
* ``415`` when ``Content-Type`` is missing or not ``audio/...``.
* ``400`` on an empty upload.
* ``503`` on ``"missing_dep"`` — the audio was readable, Whisper is
  just not installed on the server. The UI prompts the operator to
  install ``openai-whisper`` or ``faster-whisper``.
* ``500`` on ``"transcribe_failed"`` — backend was present but raised.

This module deliberately does NOT register itself with the FastAPI app
in :mod:`app.web.main` — the task spec forbids touching ``main.py``.
Wire it up with::

    from app.web.routes import voice_search as voice_search_routes
    app.include_router(voice_search_routes.router)
"""

from __future__ import annotations

from typing import Annotated, Any, Final

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from app.corpus_search import KINDS
from app.logging_setup import get_logger
from app.voice_search import transcribe_and_search
from app.web.templates_engine import templates

router = APIRouter(tags=["voice-search"])

log = get_logger("persona.voice_search.routes")

# Hard cap on the uploaded blob — matches voice-note upload and the
# notes-CSV-import cap. 5 MiB ~= 5 minutes of opus at 128 kbps, which
# is generous for a search query (typically a single short phrase).
_MAX_UPLOAD_BYTES: Final[int] = 5 * 1024 * 1024

# MIME-type guard. The browser sends whatever ``MediaRecorder.mimeType``
# resolved to (typically ``audio/webm;codecs=opus`` on Chromium or
# ``audio/ogg;codecs=opus`` on Firefox). We accept anything that starts
# with ``audio/`` so we don't have to chase the codec suffix matrix
# across UA versions.
_AUDIO_MIME_PREFIX: Final[str] = "audio/"

# Default per-source result cap. Lower than the ``/search/everything``
# page (50) because a voice query is usually a single phrase and a
# tighter cap keeps the results pane readable on mobile.
_LIMIT: Final[int] = 30


def _is_audio_mime(content_type: str | None) -> bool:
    """Return ``True`` for any ``audio/...`` content type.

    Tolerates the ``;codecs=...`` suffix by checking only the prefix.
    Empty / ``None`` MIME is treated as *not* audio so the route can
    reject ``application/octet-stream`` uploads that bypass the page UI.
    """
    if not content_type:
        return False
    return content_type.strip().lower().startswith(_AUDIO_MIME_PREFIX)


@router.post("/api/voice-search")
async def voice_search_upload(
    request: Request,
    file: Annotated[UploadFile, File(...)],
) -> JSONResponse:
    """Accept a voice-query upload, transcribe + search, return JSON.

    The route is intentionally a thin HTTP shell: all business logic
    (temp-file write, Whisper call, corpus search) lives in
    :func:`app.voice_search.transcribe_and_search`. The mapping of the
    helper's ``status`` to an HTTP status code is the only
    HTTP-specific decision the route owns.
    """
    client = request.client.host if request.client is not None else None

    mime = file.content_type or ""
    if not _is_audio_mime(mime):
        log.warning(
            "voice_search.upload.bad_mime",
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
        log.warning("voice_search.upload.empty", client=client)
        raise HTTPException(status_code=400, detail="empty upload")
    if size > _MAX_UPLOAD_BYTES:
        log.warning(
            "voice_search.upload.too_large",
            client=client,
            bytes=size,
            limit=_MAX_UPLOAD_BYTES,
        )
        raise HTTPException(status_code=413, detail="upload too large")

    result = await transcribe_and_search(raw, mime, limit=_LIMIT)
    status = result.get("status")

    if status == "missing_dep":
        log.warning(
            "voice_search.upload.missing_backend",
            client=client,
            bytes=size,
        )
        return JSONResponse(status_code=503, content=result)

    if status == "transcribe_failed":
        log.warning(
            "voice_search.upload.transcribe_failed",
            client=client,
            bytes=size,
        )
        return JSONResponse(status_code=500, content=result)

    log.info(
        "voice_search.upload.ok",
        client=client,
        bytes=size,
        status=status,
        total=result.get("total_matches"),
    )
    # Both ``"ok"`` and ``"empty_transcript"`` use HTTP 200 — the upload
    # processed fine and the UI inspects ``status`` to decide between
    # "here are your hits" and "we heard silence, try again".
    return JSONResponse(status_code=200, content=result)


@router.get("/search/voice", response_class=HTMLResponse)
async def voice_search_page(request: Request) -> HTMLResponse:
    """Render the standalone voice-search page.

    The page boots empty (no server-side search is run on GET — the
    query is the *audio*, which arrives via the subsequent POST). The
    record button is the page's centrepiece; the results pane is
    populated client-side from the ``/api/voice-search`` response.

    ``active_nav`` matches the ``search`` slug so the existing
    /search route in the top nav stays highlighted when the operator
    is on the voice variant — same family of features.
    """
    # The template iterates over :data:`KINDS` for its empty bucket
    # placeholders; passing the tuple in keeps the template free of
    # any hard-coded kind list that could drift away from the helper.
    context: dict[str, Any] = {
        "title": "Поиск голосом",
        "active_nav": "search",
        "kinds": list(KINDS),
        "limit": _LIMIT,
        "max_upload_mb": _MAX_UPLOAD_BYTES // (1024 * 1024),
    }
    return templates.TemplateResponse(request, "voice_search.html", context)


__all__ = ["router"]
