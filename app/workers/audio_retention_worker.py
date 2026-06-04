"""Tiered retention for audio segments — v1.11 feature 2/3.

Audio bytes are bulky (even Opus at 32 kbps ≈ 4 KB/s, ~14 MB/hr of
speech). The transcript is two orders of magnitude smaller and carries
*all* the searchable content. So the retention policy is asymmetric:

    * **Hot** (< ``audio_retention_hot_days``, default 7): the ``.wav``
      / ``.opus`` file stays on disk, ``audio_segment`` row points to
      it, transcript is present.
    * **Cold** (≥ ``audio_retention_hot_days``): the audio file is
      deleted, the row is rewritten with ``size_bytes = 0`` and
      ``path = ""``, and the transcript is preserved forever.

A configurable fraction of segments — ``audio_keep_sample_pct``
(default 5 %) — bypasses the purge and keeps its audio bytes. That's
the long-tail "voice signature" corpus: future speaker-identification
or voice-style work needs real audio, not just transcripts, and
sampling 5 % gives us a representative cross-section without paying
the full storage cost.

Sampling is **deterministic** on ``id`` so the decision is stable
across sweeps. If a segment was kept on one tick, it stays kept on
every subsequent tick.

Failure policy mirrors the rest of the retention workers:
    * Per-row failures are logged at ``warning`` and swallowed.
    * The polling loop itself catches and re-raises ``CancelledError``
      while logging every other exception via ``log.exception`` so a
      transient OS error never crashes the worker.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.time import iso
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

log = get_logger("persona.audio.retention")

CHECK_INTERVAL_SECONDS = 3600.0
"""One hour. Audio segments are minutes-to-hours long; sweeping every
hour is plenty granular and keeps the worker cost negligible."""

PURGE_BATCH_LIMIT = 500
"""Per-tick row cap. Bounds the worst-case sweep duration on a backlog
(e.g. first run after a long retention window change) so we don't
hold the SQLite connection for minutes."""


async def run_audio_retention_worker(
    controller: CaptureController | None = None,
) -> None:
    """Demote hot audio segments to cold once per hour.

    The hot→cold demotion deletes the audio file, zeroes ``size_bytes``,
    and blanks ``path``; the ``transcript`` column is untouched (that's
    the whole point of the asymmetric policy).
    """
    ctrl = controller or get_controller()

    while not ctrl.stop_event.is_set():
        await beat("audio-retention-worker")
        try:
            stats = await _sweep_once()
            if any(stats.values()):
                log.info("audio.retention.swept", **stats)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("audio.retention.sweep_failed", error=str(exc))

        try:
            await asyncio.wait_for(
                ctrl.stop_event.wait(),
                timeout=CHECK_INTERVAL_SECONDS,
            )
        except TimeoutError:
            continue


async def _sweep_once() -> dict[str, int]:
    """One pass over the audio_segment table. Returns purge stats."""
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(
        days=settings.audio_retention_hot_days,
    )
    data_dir = settings.data_dir

    # ``audio_keep_sample_pct`` of segments are kept by id-modulo. We
    # convert the fraction to an integer modulus so the decision is
    # cheap, stable, and SQL-expressible if we ever need to push the
    # filter into the WHERE clause. ``modulus == 0`` means "keep
    # nothing"; ``modulus == 1`` means "keep everything" (degenerate
    # but valid for testing).
    modulus, keep_remainder = _sampling_params(settings.audio_keep_sample_pct)

    # ``audio_segment.started_at`` is the wall-clock bound the worker
    # writes (migration 092). We compare against it to define the hot
    # window. ``path != ''`` excludes already-purged rows; ``size_bytes
    # > 0`` is belt-and-braces in case an older worker version left a
    # stale path with a zero size.
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT id, path, size_bytes FROM audio_segment "
                "WHERE started_at < ? "
                "AND size_bytes > 0 "
                "AND path IS NOT NULL AND path != '' "
                "ORDER BY id ASC "
                "LIMIT ?",
                (iso(cutoff), PURGE_BATCH_LIMIT),
            )
            rows = await cursor.fetchall()
    except Exception as exc:
        log.exception("audio.retention.select_failed", error=str(exc))
        return {"purged": 0, "kept_sample": 0, "bytes_freed": 0}

    purged = 0
    kept_sample = 0
    bytes_freed = 0

    for row in rows:
        seg_id = int(row["id"])
        raw_path = row["path"]
        path_str = str(raw_path) if raw_path is not None else ""
        size_bytes = int(row["size_bytes"]) if row["size_bytes"] is not None else 0

        if _is_voice_sample(seg_id, modulus, keep_remainder):
            kept_sample += 1
            log.debug(
                "audio.retention.kept_sample",
                segment_id=seg_id,
                size_bytes=size_bytes,
            )
            continue

        # ``audio_worker`` stores ``path`` as a slash-separated string
        # *relative* to ``settings.data_dir`` (see ``out_path.relative_to``
        # in :mod:`app.workers.audio_worker`); fall back to treating the
        # stored value as absolute if it's not under ``data_dir`` (older
        # rows, or a manual edit pointing outside the data tree).
        if path_str:
            resolved = _resolve_audio_path(path_str, data_dir)
            if resolved.exists():
                try:
                    resolved.unlink()
                    bytes_freed += size_bytes
                except OSError as exc:
                    log.warning(
                        "audio.retention.unlink_failed",
                        segment_id=seg_id,
                        path=str(resolved),
                        error=str(exc),
                    )
                    # Even if unlink failed (locked file, perms) we
                    # still zero the row — the user asked for the
                    # retention policy; a leftover file on disk is
                    # less bad than a row that re-points to a stale
                    # path forever. The orphan-cleanup CLI can mop
                    # it up later.

        try:
            async with get_connection() as conn:
                await conn.execute(
                    "UPDATE audio_segment "
                    "SET size_bytes = 0, path = '' "
                    "WHERE id = ?",
                    (seg_id,),
                )
                await conn.commit()
        except Exception as exc:
            log.warning(
                "audio.retention.update_failed",
                segment_id=seg_id,
                error=str(exc),
            )
            continue

        purged += 1

    return {
        "purged": purged,
        "kept_sample": kept_sample,
        "bytes_freed": bytes_freed,
    }


def _resolve_audio_path(stored: str, data_dir: Path) -> Path:
    """Return an absolute :class:`Path` for an ``audio_segment.path`` value.

    The worker normally stores paths *relative* to ``data_dir``
    (slash-separated for cross-platform stability). A handful of edge
    cases (data_dir relocations, manual edits) can leave an absolute
    path on the row; we detect both shapes here so the unlink sweep
    works either way.
    """
    candidate = Path(stored)
    if candidate.is_absolute():
        return candidate
    return data_dir / stored


def _sampling_params(keep_pct: float) -> tuple[int, int]:
    """Convert a fraction into an ``(modulus, remainder)`` pair.

    ``keep_pct`` is clamped into ``[0.0, 1.0]`` by the pydantic field
    validator, but we re-clamp defensively here so a malformed call
    site never produces a negative modulus.

    Returns ``(0, 0)`` for ``keep_pct == 0`` ("keep nothing") and
    ``(1, 0)`` for ``keep_pct == 1`` ("keep everything"). Otherwise
    the modulus is ``round(1 / keep_pct)`` and the remainder is
    fixed at ``0`` — i.e. segment ``id`` divisible by ``modulus`` is
    a voice-signature sample.
    """
    pct = max(0.0, min(1.0, keep_pct))
    if pct == 0.0:
        return 0, 0
    if pct >= 1.0:
        return 1, 0
    modulus = max(1, round(1.0 / pct))
    return modulus, 0


def _is_voice_sample(segment_id: int, modulus: int, remainder: int) -> bool:
    """Return ``True`` iff ``segment_id`` is in the voice-signature sample.

    Deterministic on ``id`` so a kept segment stays kept across
    every subsequent sweep. ``modulus == 0`` means "keep nothing"
    (purge everyone); ``modulus == 1`` means "keep everything"
    (purge no-one).
    """
    if modulus <= 0:
        return False
    if modulus == 1:
        return True
    return (segment_id % modulus) == remainder


__all__ = ["run_audio_retention_worker"]
