"""Voice-memo quick-capture — record from browser, transcribe, save as note.

A small bridge between two pre-existing subsystems:

* the v0.37 ``notes`` table (standalone markdown notes, inserted via
  :func:`app.storage.notes.insert_inbox_note`), and
* the v1.11 Whisper transcription path
  (:func:`app.audio.transcribe.transcribe_segment`).

The browser captures a short voice memo via ``MediaRecorder`` (typically
``audio/webm`` or ``audio/ogg``) and POSTs the raw bytes to the upload
route in :mod:`app.web.routes.voice_note`. That route hands the bytes
straight to :func:`ingest_voice_note`, which:

1. Picks a MIME-derived extension (``.webm`` / ``.ogg`` / ``.wav`` / …)
   and writes the bytes under ``<data_dir>/voice_notes/YYYY/MM/DD/<ts>.<ext>``.
   The same date-shard layout the audio worker uses for ``audio_segment``
   files (see :mod:`app.audio.capture`) — keeps the data root tidy when
   the user dictates a hundred memos in a busy week.
2. Calls :func:`app.audio.transcribe.transcribe_segment` to lift the
   spoken words into text. When neither Whisper backend is installed the
   transcriber returns ``None``; we surface that as a distinct
   ``missing_dep`` status so the route can answer ``503`` and the UI can
   prompt the operator to install ``openai-whisper`` or
   ``faster-whisper``.
3. Inserts one row into ``notes`` with ``title`` ``"Voice memo …"``,
   ``body`` set to the transcript, and ``source`` ``"voice_note"`` —
   matching the convention used by the inbox importer and the CSV
   importer so a search filter on ``source = 'voice_note'`` produces a
   clean stream of dictated memos.

The audio file itself is *not* tracked in the database. The transcript
is the durable content; the on-disk recording exists so a future
operator can re-transcribe with a better model or hand-verify a fuzzy
word, but its retention is governed by the same data-root janitor that
sweeps stale audio segments. A note row that loses its underlying audio
file is still useful — the text is the canonical artefact.

Return contract
---------------
:func:`ingest_voice_note` returns one of three dict shapes (always with
the same key set so the route can serialise it uniformly):

* ``{"status": "ok", "note_id": int, "transcript": str, "file_path": str}``
  — happy path; ``transcript`` may be ``""`` for an all-silence memo
  (Whisper genuinely heard nothing). The route surfaces that to the UI
  so the operator knows the upload succeeded and the recording was just
  silent.
* ``{"status": "missing_dep", "note_id": None, "transcript": None,
   "file_path": str}`` — no Whisper backend is installed. The audio file
  was still written so the operator can re-run after installing one.
* ``{"status": "transcribe_failed", "note_id": None, "transcript": None,
   "file_path": str}`` — backend was present but the transcription call
  itself raised (corrupt audio, OOM, missing ffmpeg). The on-disk file
  is kept so the operator can inspect it.

The route maps these to ``201`` / ``503`` / ``500`` respectively.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from app.audio.transcribe import transcribe_segment
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.notes import insert_inbox_note

if TYPE_CHECKING:
    from pathlib import Path

log = get_logger("persona.voice_note")

# MIME → file extension. Matches what every desktop ``MediaRecorder``
# implementation actually writes today (Chromium → webm/opus, Firefox →
# ogg/opus, Safari → mp4/aac). Anything outside this map falls back to
# ``.bin`` so the operator can still inspect the bytes; the transcriber
# either copes (ffmpeg sniffs the magic header) or fails cleanly via
# :func:`app.audio.transcribe.transcribe_segment`'s own exception
# handling.
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

# Sub-directory inside ``data_dir`` that holds the on-disk recordings.
# Mirrors the ``audio/`` shard used by ``audio_segment`` so the data
# layout stays predictable: one directory per feature, dated YYYY/MM/DD
# below it.
_SUBDIR: Final[str] = "voice_notes"

# Title prefix for the inserted ``notes`` row. The full title is
# ``"Voice memo 2026-06-04 14:31 UTC"`` (or similar). Keeping a stable
# prefix makes the day view filter "show only voice memos" trivial
# while still letting the operator eyeball the capture time at a glance.
_TITLE_PREFIX: Final[str] = "Voice memo"


def _ext_for_mime(mime: str) -> str:
    """Map a browser-supplied MIME to a safe file extension.

    Normalises the MIME (strip + lowercase + drop any ``;codecs=…``
    suffix) before lookup. ``MediaRecorder`` typically emits values like
    ``"audio/webm;codecs=opus"`` and we need the base type for the
    extension lookup.
    """
    cleaned = mime.strip().lower()
    if ";" in cleaned:
        cleaned = cleaned.split(";", 1)[0].strip()
    return _MIME_TO_EXT.get(cleaned, _FALLBACK_EXT)


def _build_target_path(now: datetime, ext: str) -> Path:
    """Compute ``<data_dir>/voice_notes/YYYY/MM/DD/<unix_ts>.<ext>``.

    Returns an absolute path; the parent directory is *not* created here
    — the caller does that right before writing so a probe of the path
    in tests doesn't leave behind empty date directories.
    """
    settings = get_settings()
    # ``now`` is always UTC (see the caller). Naming files by integer
    # unix-seconds keeps the on-disk listing sortable without ambiguity
    # across timezones, and matches the convention used elsewhere in
    # the audio pipeline.
    ts = int(now.timestamp())
    folder = settings.data_dir / _SUBDIR / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
    return folder / f"{ts}.{ext}"


def _format_title(now: datetime) -> str:
    """Render the ``notes.title`` field for the inserted row.

    ``"Voice memo 2026-06-04 14:31 UTC"`` — the timezone suffix makes
    the timestamp self-describing in the inbox even when the operator's
    display timezone differs from UTC at storage time.
    """
    return f"{_TITLE_PREFIX} {now:%Y-%m-%d %H:%M} UTC"


async def ingest_voice_note(raw_audio_bytes: bytes, mime_type: str) -> dict[str, object]:
    """Persist ``raw_audio_bytes`` to disk, transcribe, insert a note row.

    Args:
        raw_audio_bytes: The full body of the multipart upload. The route
            has already enforced the 5 MiB cap before calling us; an
            empty body is treated as a programming error and raises
            ``ValueError`` (the route should answer 400 before we ever
            see an empty payload).
        mime_type: The ``Content-Type`` of the upload as the browser
            declared it. Used purely to pick the file extension — the
            transcriber sniffs the bytes itself and does not trust the
            label.

    Returns:
        A dict with the keys ``status`` (one of ``"ok"``,
        ``"transcribe_failed"``, ``"missing_dep"``), ``note_id``
        (``int`` on the happy path, ``None`` otherwise), ``transcript``
        (``str`` on the happy path, ``None`` otherwise), and
        ``file_path`` (the absolute path of the on-disk recording, always
        present so the route can mention it in the audit log).

    Raises:
        ValueError: If ``raw_audio_bytes`` is empty. The route is
            expected to reject the upload before reaching here; a bare
            ``ValueError`` is preferable to silently inserting a zero-byte
            file into ``data_dir``.
    """
    if not raw_audio_bytes:
        msg = "raw_audio_bytes must not be empty"
        raise ValueError(msg)

    now = datetime.now(UTC)
    ext = _ext_for_mime(mime_type)
    target = _build_target_path(now, ext)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(raw_audio_bytes)
    file_path_str = str(target)

    log.info(
        "voice_note.saved",
        path=file_path_str,
        mime=mime_type,
        ext=ext,
        bytes=len(raw_audio_bytes),
    )

    transcript = await transcribe_segment(target)

    if transcript is None:
        # ``transcribe_segment`` returns ``None`` for two distinct
        # failure modes — backend missing and backend raised. We rely on
        # the structured-log line it emits to disambiguate after the
        # fact; from this layer the right next step is the same in both
        # cases: keep the audio on disk, surface a clear status so the
        # route returns a useful error to the UI.
        #
        # We distinguish the two via a follow-up probe of the resolver:
        # importing it lazily so the missing-dep branch does not depend
        # on the heavy ``whisper`` import surviving an earlier failure.
        from app.audio.transcribe import _resolve_backend  # noqa: PLC0415

        backend = _resolve_backend()
        status = "missing_dep" if backend == "none" else "transcribe_failed"
        log.warning(
            "voice_note.transcribe_unavailable",
            path=file_path_str,
            status=status,
        )
        return {
            "status": status,
            "note_id": None,
            "transcript": None,
            "file_path": file_path_str,
        }

    title = _format_title(now)
    async with get_connection() as conn:
        note_id = await insert_inbox_note(
            conn,
            body=transcript,
            title=title,
            source="voice_note",
        )

    log.info(
        "voice_note.note_inserted",
        note_id=note_id,
        chars=len(transcript),
        path=file_path_str,
    )
    return {
        "status": "ok",
        "note_id": note_id,
        "transcript": transcript,
        "file_path": file_path_str,
    }


__all__ = ["ingest_voice_note"]
