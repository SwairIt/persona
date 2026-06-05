"""Auto-bookmark long-read sessions in the screenshot stream.

v1.39 feature — when the user spends more than ``min_duration_minutes``
on the same foreground ``window_title`` without going idle for longer
than the configured ``idle_threshold_seconds``, that span is recorded
as a row in ``long_read``. The dashboard renders it as an automatic
"you read this for N minutes" bookmark so the user can jump back to
the screenshot bounds of any long focused session.

Detection algorithm (single pass over the lookback window):

1. ``SELECT id, captured_at, app_name, window_title FROM screenshots``
   in the last ``lookback_minutes`` ordered by ``captured_at`` ASC.
2. Walk the rows, maintaining an open "run" of consecutive shots that
   share the same ``window_title``. The run is closed when either the
   title changes or the gap to the next shot exceeds the configured
   ``idle_threshold_seconds`` (read from ``Settings`` /
   ``kv_settings`` via :func:`app.settings.effective.get_effective_int`
   — same source the capture loop already uses).
3. When a run closes, if its duration spans at least
   ``min_duration_minutes`` insert it into ``long_read``. The
   ``UNIQUE(started_at)`` constraint plus ``ON CONFLICT(started_at)``
   keeps re-runs idempotent.

Returns a small summary dict for the worker / API layer; the detector
itself never raises on a malformed row — bad timestamps are logged and
skipped so one corrupt screenshot can't poison the whole tick.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final, TypedDict

from app.logging_setup import get_logger
from app.settings.effective import get_effective_int
from app.storage.db import get_connection

if TYPE_CHECKING:
    from aiosqlite import Connection

log = get_logger("persona.long_read_detector")

_IDLE_THRESHOLD_SETTING: Final[str] = "idle_threshold_seconds"
_IDLE_THRESHOLD_FALLBACK_SECONDS: Final[int] = 300


class LongReadDetectionResult(TypedDict):
    """Summary of a single detector tick."""

    detected: int
    inserted: int
    skipped_too_short: int


@dataclass
class _OpenRun:
    """Mutable bookkeeping for an in-progress consecutive-shot session."""

    first_id: int
    last_id: int
    first_at: datetime
    last_at: datetime
    title: str
    app_name: str | None


def _parse_iso(value: str | None) -> datetime | None:
    """Parse ``captured_at`` ISO-8601 timestamps, naive-tolerant.

    Screenshots are persisted with whatever format the capture loop
    emits — either ``YYYY-MM-DDTHH:MM:SS`` (naive UTC, the legacy
    default) or ``...+00:00`` (current). Anything else is logged at
    debug and skipped — duration arithmetic on bad data is worse than
    dropping the row.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        log.debug("long_read_detector.bad_timestamp", value=value[:64])
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def _emit_run(
    conn: Connection,
    run: _OpenRun,
    *,
    min_duration_seconds: int,
) -> tuple[bool, bool]:
    """Insert ``run`` if it meets the duration threshold.

    Returns ``(qualified, inserted)`` so the caller can update its
    detected / inserted / skipped counters in one place.
    """
    duration = int((run.last_at - run.first_at).total_seconds())
    if duration < min_duration_seconds:
        return False, False
    cursor = await conn.execute(
        "INSERT INTO long_read ("
        "window_title, app_name, started_at, ended_at, "
        "duration_seconds, screenshot_id_first, screenshot_id_last"
        ") VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(started_at) DO NOTHING",
        (
            run.title,
            run.app_name,
            run.first_at.isoformat(),
            run.last_at.isoformat(),
            duration,
            run.first_id,
            run.last_id,
        ),
    )
    return True, (cursor.rowcount or 0) > 0


async def detect_long_reads(
    lookback_minutes: int = 60,
    min_duration_minutes: int = 5,
) -> LongReadDetectionResult:
    """Scan the last ``lookback_minutes`` for long-read sessions.

    Args:
        lookback_minutes: How far back to look for completed sessions.
            One hour is enough for the 600 s poll cadence used by
            :mod:`app.workers.long_read_worker` — the runner re-detects
            the same boundaries every tick and the ``UNIQUE(started_at)``
            constraint makes that safe.
        min_duration_minutes: Minimum span for a run to qualify as a
            long-read. Anything shorter is counted in
            ``skipped_too_short`` but not inserted.

    Returns:
        :class:`LongReadDetectionResult` with three counters:
        ``detected`` — runs that met the duration threshold;
        ``inserted`` — rows actually written (excludes already-present
        ``started_at`` collisions); ``skipped_too_short`` — runs found
        but rejected for being below the threshold.
    """
    idle_threshold_seconds = await get_effective_int(
        _IDLE_THRESHOLD_SETTING, default=_IDLE_THRESHOLD_FALLBACK_SECONDS
    )
    if idle_threshold_seconds <= 0:
        idle_threshold_seconds = _IDLE_THRESHOLD_FALLBACK_SECONDS

    min_duration_seconds = max(0, min_duration_minutes * 60)
    window_start = datetime.now(tz=UTC) - timedelta(minutes=lookback_minutes)

    detected = 0
    inserted = 0
    skipped_too_short = 0

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, captured_at, app_name, window_title "
            "FROM screenshots "
            "WHERE captured_at >= ? "
            "ORDER BY captured_at ASC",
            (window_start.isoformat(),),
        )
        rows = await cursor.fetchall()

        async def _flush(run: _OpenRun | None) -> None:
            nonlocal detected, inserted, skipped_too_short
            if run is None:
                return
            qualified, did_insert = await _emit_run(
                conn, run, min_duration_seconds=min_duration_seconds
            )
            if not qualified:
                skipped_too_short += 1
                return
            detected += 1
            if did_insert:
                inserted += 1

        open_run: _OpenRun | None = None
        for row in rows:
            title_raw = row["window_title"]
            title = str(title_raw) if title_raw is not None else ""
            if not title:
                # Untitled windows cannot anchor a session.
                await _flush(open_run)
                open_run = None
                continue

            captured = _parse_iso(row["captured_at"])
            if captured is None:
                continue

            shot_id = int(row["id"])
            app_raw = row["app_name"]
            app_name = str(app_raw) if app_raw is not None else None

            if open_run is None:
                open_run = _OpenRun(
                    first_id=shot_id,
                    last_id=shot_id,
                    first_at=captured,
                    last_at=captured,
                    title=title,
                    app_name=app_name,
                )
                continue

            gap_seconds = (captured - open_run.last_at).total_seconds()
            if title == open_run.title and gap_seconds <= idle_threshold_seconds:
                open_run.last_id = shot_id
                open_run.last_at = captured
                continue

            await _flush(open_run)
            open_run = _OpenRun(
                first_id=shot_id,
                last_id=shot_id,
                first_at=captured,
                last_at=captured,
                title=title,
                app_name=app_name,
            )

        await _flush(open_run)
        await conn.commit()

    result: LongReadDetectionResult = {
        "detected": detected,
        "inserted": inserted,
        "skipped_too_short": skipped_too_short,
    }
    log.info(
        "long_read_detector.tick",
        lookback_minutes=lookback_minutes,
        min_duration_minutes=min_duration_minutes,
        idle_threshold_seconds=idle_threshold_seconds,
        detected=detected,
        inserted=inserted,
        skipped_too_short=skipped_too_short,
    )
    return result


__all__ = ["LongReadDetectionResult", "detect_long_reads"]
