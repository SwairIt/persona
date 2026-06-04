"""Speech-only audio capture worker (v1.11 features 1/3 + 2/3).

Wires the four :mod:`app.audio` building blocks into a single async
loop:

    1. :func:`app.audio.capture.record_chunk` — pull a fresh 30 s mono
       chunk off the microphone ring buffer.
    2. :func:`app.audio.vad.detect_speech_segments` — list of
       ``(start_s, end_s)`` speech windows inside the chunk.
    3. :func:`app.audio.preprocess.preprocess` — band-pass + EBU R128
       normalisation, applied to each speech window.
    4. :func:`app.audio.encode.encode_segment` — Encodec (preferred) /
       Opus / ffmpeg, returning ``(codec_name, encoded_bytes)``.
    5. Write the bytes to ``data/audio/YYYY/MM/DD/{uuid}.{ext}`` and
       insert a row into ``audio_segment`` (migration 092).
    6. v1.11 feature 2/3 — call :func:`app.audio.transcribe.transcribe_segment`
       on the freshly-written file and ``UPDATE audio_segment SET
       transcript = ?`` with the result. Whisper-derived text is the
       *lossless content* track: it survives the audio-file purge run
       by :mod:`app.workers.audio_retention_worker` after
       ``settings.audio_retention_hot_days`` days, so search still
       works long after the bytes are gone. When neither
       ``openai-whisper`` nor ``faster-whisper`` is installed the
       transcribe call returns ``None`` and the column stays ``NULL`` —
       graceful degradation, not a failure.

Defensive policy mirrors :mod:`app.workers.clipboard_worker` and the
embeddings worker — when ``settings.audio_capture_enabled`` is False
*or* any required dep is missing, the worker logs a single info line
and awaits ``stop_event`` without spinning. Per-iteration exceptions
are caught + logged at ``exception`` so a transient mic disconnect
doesn't bring down the whole worker.

Output naming:
    Each segment is keyed by a UUID4 (``shot_id``) so two segments
    started in the same millisecond never collide on disk. The
    extension is derived from the codec — ``.encodec`` for Meta's
    neural codec, ``.opus`` for everything else.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from app.audio.capture import SAMPLE_RATE, record_chunk
from app.audio.encode import (
    ENCODEC_BITRATE_KBPS,
    OPUS_BITRATE_BPS,
    encode_segment,
)
from app.audio.preprocess import preprocess
from app.audio.transcribe import transcribe_segment
from app.audio.vad_facade import detect_speech_segments
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.time import iso
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

if TYPE_CHECKING:  # pragma: no cover — types only
    import numpy as np

log = get_logger("persona.audio.worker")

POLL_INTERVAL_SECONDS = 1.0
"""Sleep between iterations *when no chunk is ready*. ``record_chunk``
itself blocks until the ring buffer has a full 30 s slice, so the loop
is almost entirely paced by capture, not this constant."""


_CODEC_EXTENSIONS: dict[str, str] = {
    "encodec": "encodec",
    "opus": "opus",
    "opus_ffmpeg": "opus",
    "missing_dep": "bin",
}


def _codec_bitrate(codec: str) -> int | None:
    """Return the nominal bitrate (bps) for a codec name, or ``None``."""
    if codec == "encodec":
        # Encodec is configured for 1.5 kbps in
        # :data:`app.audio.encode.ENCODEC_BITRATE_KBPS`.
        return int(ENCODEC_BITRATE_KBPS * 1_000)
    if codec in {"opus", "opus_ffmpeg"}:
        return OPUS_BITRATE_BPS
    return None


def _segment_path(
    *,
    data_dir: Path,
    started_at: datetime,
    codec: str,
    shot_id: str,
) -> Path:
    """Build the on-disk path ``audio/YYYY/MM/DD/{shot_id}.{ext}``.

    Returns an *absolute* path; the caller may store it as-is or
    re-relativise against ``data_dir`` before persisting — the v1.11
    feature 3/3 streaming route resolves both shapes.
    """
    ext = _CODEC_EXTENSIONS.get(codec, "bin")
    day = started_at.astimezone(UTC)
    sub = Path(f"{day.year:04d}") / f"{day.month:02d}" / f"{day.day:02d}"
    full = data_dir / "audio" / sub
    full.mkdir(parents=True, exist_ok=True)
    return full / f"{shot_id}.{ext}"


async def run_audio_worker(controller: CaptureController | None = None) -> None:
    """Drive the speech-only capture loop until ``stop_event`` fires.

    Hard-gated by ``settings.audio_capture_enabled`` (default ``False``
    for privacy). When the flag is off the worker awaits the stop event
    without ever touching the microphone — flipping the toggle requires
    a restart, matching the embeddings / clipboard worker semantics.
    """
    ctrl = controller or get_controller()
    settings = get_settings()

    if not settings.audio_capture_enabled:
        log.info("audio_worker.disabled", reason="setting_off")
        await ctrl.stop_event.wait()
        return

    log.info(
        "audio_worker.started",
        preferred_codec=settings.audio_preferred_codec,
        vad_threshold=settings.audio_vad_threshold,
    )

    while not ctrl.stop_event.is_set():
        await beat("audio-worker")
        try:
            await _drain_once()
        except asyncio.CancelledError:
            log.info("audio_worker.cancelled")
            raise
        except Exception as exc:
            log.exception("audio_worker.iteration_failed", error=str(exc))

        try:
            await asyncio.wait_for(
                ctrl.stop_event.wait(),
                timeout=POLL_INTERVAL_SECONDS,
            )
        except TimeoutError:
            continue

    log.info("audio_worker.stopped")


async def _drain_once() -> None:
    """Capture → VAD → preprocess → encode → persist for one 30 s chunk."""
    chunk = await record_chunk()
    if isinstance(chunk, dict):
        # ``record_chunk`` returns a status dict on missing-dep paths
        # (sounddevice unavailable, no input device, etc). Log once per
        # iteration — the warning surfaces in the dashboard without
        # taking down the worker.
        log.warning("audio_worker.capture_unavailable", **chunk)
        return

    chunk_started_at = datetime.now(UTC) - timedelta(
        seconds=float(chunk.shape[0]) / float(SAMPLE_RATE),
    )

    segments = await detect_speech_segments(chunk, SAMPLE_RATE)
    if isinstance(segments, dict):
        log.warning("audio_worker.vad_unavailable", **segments)
        return

    if not segments:
        log.debug("audio_worker.no_speech", samples=int(chunk.shape[0]))
        return

    settings = get_settings()
    data_dir = settings.data_dir

    for start_s, end_s in segments:
        try:
            await _persist_speech_segment(
                chunk=chunk,
                start_s=start_s,
                end_s=end_s,
                chunk_started_at=chunk_started_at,
                data_dir=data_dir,
            )
        except Exception as exc:
            log.exception(
                "audio_worker.segment_failed",
                start_s=start_s,
                end_s=end_s,
                error=str(exc),
            )


async def _persist_speech_segment(
    *,
    chunk: np.ndarray,
    start_s: float,
    end_s: float,
    chunk_started_at: datetime,
    data_dir: Path,
) -> None:
    """Slice + preprocess + encode + persist a single speech window."""
    settings = get_settings()
    start_idx = max(0, int(start_s * SAMPLE_RATE))
    end_idx = min(int(chunk.shape[0]), int(end_s * SAMPLE_RATE))
    if end_idx <= start_idx:
        return

    raw_segment = chunk[start_idx:end_idx]
    cleaned = preprocess(raw_segment, sample_rate=SAMPLE_RATE)

    codec, encoded_bytes = encode_segment(
        cleaned,
        SAMPLE_RATE,
        preferred=settings.audio_preferred_codec,
    )
    if not encoded_bytes:
        log.warning(
            "audio_worker.encode_empty",
            codec=codec,
            duration_s=end_s - start_s,
        )
        return

    shot_id = uuid.uuid4().hex
    started_at = chunk_started_at + timedelta(seconds=start_s)
    ended_at = chunk_started_at + timedelta(seconds=end_s)
    duration_s = float(end_s - start_s)

    out_path = _segment_path(
        data_dir=data_dir,
        started_at=started_at,
        codec=codec,
        shot_id=shot_id,
    )

    try:
        await asyncio.to_thread(out_path.write_bytes, encoded_bytes)
    except OSError as exc:
        log.warning(
            "audio_worker.write_failed",
            path=str(out_path),
            error=str(exc),
        )
        return

    size_bytes = len(encoded_bytes)
    bitrate = _codec_bitrate(codec) or settings.audio_target_bitrate

    # Persist relative-to-data_dir path so the row stays portable across
    # ``data_dir`` relocations — the v1.11 feature 3/3 streaming route
    # joins this back against the current ``settings.data_dir`` before
    # serving the bytes.
    try:
        rel_path = out_path.relative_to(data_dir)
        stored_path = str(rel_path).replace("\\", "/")
    except ValueError:
        # Fallback — if for some reason the path is outside data_dir
        # (env mid-rotation, custom mount), store the absolute path so
        # nothing is silently lost.
        stored_path = str(out_path)

    segment_id = await _insert_segment_row(
        started_at=iso(started_at),
        ended_at=iso(ended_at),
        duration_s=duration_s,
        codec=codec,
        bitrate=bitrate,
        path=stored_path,
        size_bytes=size_bytes,
    )

    log.info(
        "audio_worker.segment_persisted",
        codec=codec,
        duration_s=round(duration_s, 3),
        bytes=size_bytes,
        path=stored_path,
    )

    # v1.13 — count toward the daily byte budget. Failure MUST NOT break
    # the audio worker; budget is a guidance signal, not a hard fence.
    try:
        from app import budget as _budget  # noqa: PLC0415

        await _budget.add_bytes("audio", size_bytes)
    except Exception as exc:  # noqa: BLE001
        log.debug("audio_worker.budget_bump_failed", error=str(exc))

    # v1.11 feature 2/3 — lossless content track. We transcribe *after*
    # the row is committed so a Whisper failure (model load OOM, corrupt
    # WAV, missing backend) never blocks segment persistence. The audio
    # file is on disk, the row exists; only the ``transcript`` column
    # may stay NULL until a retroactive backfill. When neither
    # ``openai-whisper`` nor ``faster-whisper`` is installed
    # :func:`transcribe_segment` short-circuits to ``None`` and logs a
    # single ``audio.transcribe.no_backend`` warning on first call.
    if segment_id is None:
        return
    transcript = await transcribe_segment(out_path)
    if transcript is not None:
        await _store_transcript(segment_id, transcript)


async def _insert_segment_row(
    *,
    started_at: str,
    ended_at: str,
    duration_s: float,
    codec: str,
    bitrate: int | None,
    path: str,
    size_bytes: int,
) -> int | None:
    """Insert one row into ``audio_segment`` and return its ``id``.

    Per-spec schema (migration 092). Returns ``None`` on any insert
    failure so the caller can skip the v1.11 feature 2/3 transcription
    step — there's no row to attach the transcript to.
    """
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "INSERT INTO audio_segment "
                "(started_at, ended_at, duration_s, codec, bitrate, path, size_bytes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    started_at,
                    ended_at,
                    float(duration_s),
                    codec,
                    bitrate,
                    path,
                    int(size_bytes),
                ),
            )
            await conn.commit()
            return int(cursor.lastrowid) if cursor.lastrowid is not None else None
    except Exception as exc:
        log.warning(
            "audio_worker.insert_failed",
            error=str(exc),
            path=path,
        )
        return None


async def _store_transcript(segment_id: int, transcript: str) -> None:
    """Persist the Whisper transcript on the existing ``audio_segment`` row.

    v1.11 feature 2/3 — best-effort side-channel. The row already
    exists with the audio on disk, so a failure here means the user
    loses the transcript but keeps the audio — the opposite trade-off
    would be worse (orphan files with no row to find them by). An
    empty-string transcript (``""``) is meaningful: it distinguishes
    *"transcribed but silent"* from *"backend missing / failed"* and is
    persisted as-is so the retention worker's ``transcript IS NOT NULL``
    queries can still filter on it.
    """
    try:
        async with get_connection() as conn:
            await conn.execute(
                "UPDATE audio_segment SET transcript = ? WHERE id = ?",
                (transcript, segment_id),
            )
            await conn.commit()
    except Exception as exc:
        log.warning(
            "audio_worker.transcript_update_failed",
            segment_id=segment_id,
            error=str(exc),
        )
        return

    log.info(
        "audio_worker.transcript_stored",
        segment_id=segment_id,
        chars=len(transcript),
    )


__all__ = ["run_audio_worker"]
