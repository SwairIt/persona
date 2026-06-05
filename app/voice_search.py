"""Voice-driven corpus search — speak a query, get OCR hits.

Built on top of two pre-existing pillars:

* the v1.11 Whisper transcription helper
  (:func:`app.audio.transcribe.transcribe_segment`), already battle-tested
  by v1.36 voice-note upload (:mod:`app.voice_note`) and v1.47 voice
  dictation (:mod:`app.journal_voice`), and
* the v1.49 corpus search facade
  (:func:`app.corpus_search.corpus_search`), which already returns the
  full six-bucket ``{shots, notes, annotations, stickies, clipboard,
  audio}`` dict every search UI in the app consumes.

The flow:

1. Browser captures a short audio clip via ``MediaRecorder`` and POSTs
   the raw bytes to :mod:`app.web.routes.voice_search`.
2. The route reads the bytes (with the same 5 MiB cap as
   ``/api/voice-note/upload``) and forwards them to
   :func:`transcribe_and_search`.
3. We write the bytes to a *temporary* file (NamedTemporaryFile under
   the OS temp dir — we do NOT persist the audio under ``data_dir``;
   the query is ephemeral and the transcript+result set is the only
   thing the operator cares about), feed the path to
   ``transcribe_segment``, then unlink the temp file.
4. The transcript becomes the query string for ``corpus_search``;
   the resulting six-kind dict is returned alongside the transcript
   and a flat ``total_matches`` count for the UI's "N results across
   M sources" affordance.

Return contract
---------------
Always a dict. On the happy path::

    {
        "status":        "ok",
        "transcript":    "deploy the staging branch",
        "results":       {<six-bucket corpus_search dict>},
        "total_matches": 42,
    }

On a silent recording (Whisper returned the empty string)::

    {
        "status":        "empty_transcript",
        "transcript":    "",
        "results":       {<six empty buckets>},
        "total_matches": 0,
    }

When no Whisper backend is installed (or backend crashed)::

    {
        "status":        "missing_dep" | "transcribe_failed",
        "transcript":    None,
        "results":       {<six empty buckets>},
        "total_matches": 0,
    }

The route maps these to HTTP 200 / 200 / 503 / 500 respectively.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Final

from app.audio.transcribe import transcribe_segment
from app.corpus_search import KINDS, corpus_search
from app.logging_setup import get_logger

log = get_logger("persona.voice_search")

# MIME → file extension. Mirrors the table used by :mod:`app.voice_note`
# so a browser that records ``audio/webm;codecs=opus`` for both the
# voice-note flow and the voice-search flow produces files with the
# same extension. Whisper sniffs the magic header anyway — the suffix
# matters only because ffmpeg, the openai-whisper loader, and the
# faster-whisper loader all prefer to pick a demuxer by extension.
_MIME_TO_EXT: Final[dict[str, str]] = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/oga": "ogg",
    "audio/opus": "opus",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "mp4",
    "audio/aac": "aac",
    "audio/x-m4a": "m4a",
    "audio/flac": "flac",
}
_FALLBACK_EXT: Final[str] = "bin"


def _empty_results() -> dict[str, list[dict[str, Any]]]:
    """Return a six-bucket dict with empty lists for every kind.

    Mirrors :func:`app.corpus_search._empty_result` so callers (the
    route's failure branches, the template's iteration) never have to
    branch on a missing key. The :data:`KINDS` constant is the single
    source of truth for the key set.
    """
    return {kind: [] for kind in KINDS}


def _ext_for_mime(mime: str) -> str:
    """Map a browser-supplied MIME to a safe file extension.

    Normalises the MIME (strip + lowercase + drop any ``;codecs=…``
    suffix) before lookup. ``MediaRecorder`` typically emits values like
    ``"audio/webm;codecs=opus"`` and we need the base type for the
    extension lookup. Unknown MIMEs fall back to ``.bin``; ffmpeg's
    container sniffer copes with the bytes either way.
    """
    cleaned = mime.strip().lower()
    if ";" in cleaned:
        cleaned = cleaned.split(";", 1)[0].strip()
    return _MIME_TO_EXT.get(cleaned, _FALLBACK_EXT)


async def transcribe_and_search(
    raw_audio_bytes: bytes,
    mime_type: str,
    limit: int = 30,
) -> dict[str, Any]:
    """Transcribe ``raw_audio_bytes`` and run the transcript as a corpus search.

    Args:
        raw_audio_bytes: Full body of the multipart upload. The route is
            expected to have already enforced the 5 MiB cap and the
            audio/* MIME check before calling us; an empty body raises
            ``ValueError`` so a programming error surfaces loudly.
        mime_type: ``Content-Type`` of the upload as the browser
            declared it. Used purely to pick the temp-file extension —
            ffmpeg / Whisper sniff the bytes themselves.
        limit: Per-source row cap forwarded to
            :func:`app.corpus_search.corpus_search`. Defaults to 30 —
            a third of the ``/search/everything`` page size, because a
            voice query is usually a shorter, more targeted phrase and
            a tighter cap keeps the results pane readable on mobile.

    Returns:
        A dict with the keys ``status`` (one of ``"ok"``,
        ``"empty_transcript"``, ``"missing_dep"``, ``"transcribe_failed"``),
        ``transcript`` (``str`` on the happy / empty paths, ``None``
        otherwise), ``results`` (the six-bucket
        :func:`corpus_search` dict; empty buckets on every failure
        branch), and ``total_matches`` (``int``, the sum of the bucket
        lengths — zero on every failure branch).

    Raises:
        ValueError: If ``raw_audio_bytes`` is empty. The route is
            expected to reject the upload with 400 before we ever see
            an empty payload; raising here surfaces the programming
            error rather than silently transcribing a zero-byte file.
    """
    if not raw_audio_bytes:
        msg = "raw_audio_bytes must not be empty"
        raise ValueError(msg)

    ext = _ext_for_mime(mime_type)

    # NamedTemporaryFile with ``delete=False`` so we control the unlink
    # ourselves — on Windows the OS will refuse to delete an open file
    # while another handle (the Whisper loader's ffmpeg subprocess) is
    # reading from it. Wrapping the inference call in try/finally keeps
    # the cleanup robust even when the backend raises.
    with tempfile.NamedTemporaryFile(
        prefix="voice_search_",
        suffix=f".{ext}",
        delete=False,
    ) as tmp:
        tmp.write(raw_audio_bytes)
        tmp_path = Path(tmp.name)

    log.info(
        "voice_search.audio_saved",
        path=str(tmp_path),
        mime=mime_type,
        ext=ext,
        bytes=len(raw_audio_bytes),
    )

    try:
        transcript = await transcribe_segment(tmp_path)
    finally:
        # Best-effort unlink — the operator does not benefit from
        # leaving the temp recording on disk (we never write it under
        # ``data_dir`` and never insert a notes row). A residual file
        # in ``%TEMP%`` is harmless; logging the failure is enough.
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning(
                "voice_search.temp_unlink_failed",
                path=str(tmp_path),
                error=str(exc),
            )

    if transcript is None:
        # ``transcribe_segment`` returns ``None`` for two distinct
        # failure modes — backend missing and backend raised. The
        # follow-up probe (cheap, cached at module scope after first
        # call) lets us tell them apart so the route picks 503 vs 500.
        # Imported lazily for the same reason :mod:`app.voice_note`
        # does it: a missing-dep diagnosis must not itself crash on a
        # missing whisper.
        from app.audio.transcribe import _resolve_backend  # noqa: PLC0415

        backend = _resolve_backend()
        status = "missing_dep" if backend == "none" else "transcribe_failed"
        log.warning("voice_search.transcribe_unavailable", status=status)
        return {
            "status": status,
            "transcript": None,
            "results": _empty_results(),
            "total_matches": 0,
        }

    if not transcript.strip():
        # Whisper genuinely heard nothing. Distinguish from the
        # missing-dep / failed branches so the UI can show "We heard
        # silence — try again" instead of "Server is broken".
        log.info("voice_search.empty_transcript")
        return {
            "status": "empty_transcript",
            "transcript": "",
            "results": _empty_results(),
            "total_matches": 0,
        }

    results = await corpus_search(transcript, limit=limit)
    total = sum(len(results[kind]) for kind in KINDS)

    log.info(
        "voice_search.done",
        transcript_chars=len(transcript),
        total=total,
        limit=limit,
    )
    return {
        "status": "ok",
        "transcript": transcript,
        "results": results,
        "total_matches": total,
    }


__all__ = ["transcribe_and_search"]
