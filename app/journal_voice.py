"""Voice dictation into the journal — browser mic → Whisper → ``notes`` row.

A thin sibling of :mod:`app.voice_note`. The v1.36 voice-memo pipeline
writes a row labelled ``source = 'voice_note'`` into the standalone
``notes`` table and parks the audio under ``data_dir/voice_notes/…``.
This module reuses the *same* table and the *same* transcription helper
but tags the row with ``source = 'journal_voice'`` so the journal view
(and any future filter on ``source = 'journal_voice'``) can isolate
dictated entries from generic inbox voice memos.

Why a separate module
---------------------
We deliberately do not call :func:`app.voice_note.ingest_voice_note`
from here even though the shape is similar. Two reasons:

* That function hard-codes ``source = "voice_note"`` and a title prefix
  of ``"Voice memo …"``. Reusing it would either require widening its
  signature (and risk breaking the v1.36 pipeline that other callers
  depend on) or post-patching the inserted row, which is uglier than
  reusing the underlying transcription primitive directly.
* The journal entry semantics differ slightly — the title prefix is
  ``"Journal voice …"`` so a glance at ``/notes`` makes the origin
  obvious; the dictated transcript is the canonical artefact and the
  on-disk audio is purely a forensic backup, same as voice notes.

We do reuse the heavy machinery — :func:`app.audio.transcribe.transcribe_segment`
for the Whisper call and the same date-shard layout under
``<data_dir>/journal_voice/YYYY/MM/DD/<unix_ts>.<ext>`` — so the cost of
this module is one ``INSERT`` and one filesystem write per dictation.

Return contract
---------------
:func:`ingest_journal_dictation` always returns a dict with the keys
``status``, ``note_id``, ``transcript``, and ``char_count`` so the route
can serialise the response without branching on shape:

* ``{"status": "ok", "note_id": int, "transcript": str, "char_count": int}``
  — happy path. ``transcript`` may be ``""`` (silent recording);
  ``char_count`` is ``len(transcript)``.
* ``{"status": "missing_dep", "note_id": None, "transcript": None,
   "char_count": 0}`` — no Whisper backend installed. The audio file is
  still on disk so the operator can re-dictate after fixing the env.
* ``{"status": "transcribe_failed", "note_id": None, "transcript": None,
   "char_count": 0}`` — backend present but inference raised.

The route maps these to ``201`` / ``503`` / ``500``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from app.audio.transcribe import transcribe_segment
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection

if TYPE_CHECKING:
    from pathlib import Path

log = get_logger("persona.journal_voice")

# MIME → file extension. Same matrix as :mod:`app.voice_note` —
# duplicating the small table keeps the two modules independent (no
# private import from a sibling) and survives a future split where the
# v1.36 module gets retired without touching journal dictation.
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

# Sub-directory inside ``data_dir`` — kept separate from
# ``voice_notes/`` so an operator browsing the data root can tell
# inbox-style voice memos from journal dictations at a glance.
_SUBDIR: Final[str] = "journal_voice"

# Title prefix for the inserted ``notes`` row. The full title is
# ``"Journal voice 2026-06-05 14:31 UTC"``. Stable prefix → trivial
# filter in the inbox; UTC suffix → no timezone ambiguity when the
# operator's display zone differs from storage time.
_TITLE_PREFIX: Final[str] = "Journal voice"

# Stored verbatim in ``notes.source`` — the journal route looks for this
# literal when rendering the dictation section, and analytics queries
# can ``WHERE source = 'journal_voice'`` to isolate dictated entries.
_NOTE_SOURCE: Final[str] = "journal_voice"


def _ext_for_mime(mime: str) -> str:
    """Normalise ``mime`` (strip + lowercase + drop ``;codecs=…``) → extension.

    ``MediaRecorder`` typically emits values like
    ``"audio/webm;codecs=opus"`` — we want the base type for the lookup.
    """
    cleaned = mime.strip().lower()
    if ";" in cleaned:
        cleaned = cleaned.split(";", 1)[0].strip()
    return _MIME_TO_EXT.get(cleaned, _FALLBACK_EXT)


def _build_target_path(now: datetime, ext: str) -> Path:
    """Compute ``<data_dir>/journal_voice/YYYY/MM/DD/<unix_ts>.<ext>``.

    Absolute path; the parent directory is *not* created here — the
    caller mkdirs right before writing so a probe in tests doesn't leave
    empty date directories behind.
    """
    settings = get_settings()
    ts = int(now.timestamp())
    folder = settings.data_dir / _SUBDIR / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
    return folder / f"{ts}.{ext}"


def _format_title(now: datetime) -> str:
    """Render ``notes.title`` — ``"Journal voice 2026-06-05 14:31 UTC"``."""
    return f"{_TITLE_PREFIX} {now:%Y-%m-%d %H:%M} UTC"


async def ingest_journal_dictation(
    raw_audio_bytes: bytes,
    mime_type: str,
) -> dict[str, object]:
    """Persist a dictation: write audio, transcribe, append a note row.

    Args:
        raw_audio_bytes: The full body of the multipart upload. The
            route has already enforced the 5 MiB cap; an empty payload
            raises ``ValueError`` — the route should answer 400 before
            we ever see it.
        mime_type: The browser-declared ``Content-Type``. Used purely
            to pick the on-disk extension; the transcriber sniffs the
            bytes itself.

    Returns:
        A dict with the keys ``status`` (``"ok"`` /
        ``"transcribe_failed"`` / ``"missing_dep"``), ``note_id``
        (``int`` on success, ``None`` otherwise), ``transcript`` (the
        Whisper output, ``None`` on failure), and ``char_count`` —
        ``len(transcript)`` on success, ``0`` otherwise.

    Raises:
        ValueError: When ``raw_audio_bytes`` is empty. A bare
            ``ValueError`` is preferable to silently inserting a
            zero-byte file under ``data_dir``.
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
        "journal_voice.saved",
        path=file_path_str,
        mime=mime_type,
        ext=ext,
        bytes=len(raw_audio_bytes),
    )

    transcript = await transcribe_segment(target)

    if transcript is None:
        # ``transcribe_segment`` returns ``None`` for "backend missing"
        # *and* "backend raised". Disambiguate via a follow-up probe so
        # the route can return 503 vs 500 — the resolver is cheap and
        # cached after first call.
        from app.audio.transcribe import _resolve_backend  # noqa: PLC0415

        backend = _resolve_backend()
        status = "missing_dep" if backend == "none" else "transcribe_failed"
        log.warning(
            "journal_voice.transcribe_unavailable",
            path=file_path_str,
            status=status,
        )
        return {
            "status": status,
            "note_id": None,
            "transcript": None,
            "char_count": 0,
        }

    title = _format_title(now)
    async with get_connection() as conn:
        # Parametrised SQL — never interpolate user-derived text. The
        # ``source`` literal is a constant so it can't be tampered with
        # via the audio payload either.
        cursor = await conn.execute(
            "INSERT INTO notes (title, body, source) VALUES (?, ?, ?)",
            (title, transcript, _NOTE_SOURCE),
        )
        await conn.commit()
        row_id = cursor.lastrowid

    if row_id is None:
        # AUTOINCREMENT on SQLite always populates ``lastrowid``; a
        # ``None`` here means a driver-level surprise. Surface loudly
        # rather than returning a nonsense ``note_id``.
        msg = "INSERT INTO notes did not return a row id"
        raise RuntimeError(msg)

    note_id = int(row_id)
    char_count = len(transcript)

    log.info(
        "journal_voice.note_inserted",
        note_id=note_id,
        chars=char_count,
        path=file_path_str,
    )
    return {
        "status": "ok",
        "note_id": note_id,
        "transcript": transcript,
        "char_count": char_count,
    }


__all__ = ["ingest_journal_dictation"]
