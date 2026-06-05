"""Capture-session detection — partition screenshots into work blocks (v1.42).

A *capture session* is a maximal contiguous run of rows in
``screenshots`` such that every adjacent pair is separated by at most
``gap_threshold_seconds`` (default 30 minutes). The function
:func:`detect_sessions` scans the recent past (default last 24 hours),
walks the rows in chronological order, splits them on the gap
threshold, and writes one ``capture_session`` row per detected block.

Persistence is idempotent: each insert uses ``ON CONFLICT(started_at)
DO NOTHING`` so re-running the detector after a few more frames have
landed re-detects the same session (now slightly extended) and the
duplicate insert is silently swallowed. A future "stale session
refresh" pass could ``UPDATE`` instead of skipping — for now the
"first stamp wins" rule is good enough because the only column that
ever grows is ``ended_at``, and the UI re-derives a fresh active block
from the live screenshots table whenever the user opens ``/sessions``.

The session row also folds in:

* ``dominant_app``  — the mode of ``app_name`` across the block. NULL
  if every shot has NULL app_name (e.g. cold-boot before active-window
  probing kicks in).
* ``screen_count``  — number of screenshot rows in the block.
* ``voice_seconds`` — sum of ``audio_segment.duration_seconds`` whose
  ``captured_at`` (post-migration 093 rename) falls inside the block.
* ``top_titles_json`` — JSON array of the up-to-five most frequent
  ``window_title`` strings, sorted by count desc.

All SQL is parametrised; no string interpolation of user data.
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Final, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.capture_sessions")


_DEFAULT_LOOKBACK_HOURS: Final[int] = 24
"""How far back :func:`detect_sessions` scans by default."""

_DEFAULT_GAP_THRESHOLD_SECONDS: Final[int] = 1800
"""30 minutes — anything bigger than this closes the current session."""

_TOP_TITLES_LIMIT: Final[int] = 5
"""Number of window titles persisted in ``top_titles_json``."""


class DetectStats(TypedDict):
    """Return shape of :func:`detect_sessions`."""

    detected: int
    """How many contiguous sessions the walk found in the lookback window."""

    inserted: int
    """How many fresh rows were written (i.e. NOT duplicates)."""

    skipped_duplicates: int
    """``detected - inserted`` — sessions whose ``started_at`` already exists."""


class _Shot(TypedDict):
    """In-memory view of a screenshots row used by the partitioner."""

    id: int
    captured_at: datetime
    app_name: str | None
    window_title: str | None


def _parse_iso(value: str) -> datetime | None:
    """Tolerantly parse the legacy + current screenshot timestamp formats.

    Older capture-loop releases wrote naive UTC stamps; the current
    loop writes ``...+00:00``. We coerce both to aware UTC so gap
    arithmetic is well-defined regardless of which mix the table holds.
    Anything we cannot parse is dropped from the walk with a debug log
    — one malformed row should not break a 24-hour pass.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        log.debug("capture_sessions.bad_timestamp", value=value[:64])
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _partition(
    shots: list[_Shot],
    gap_threshold_seconds: int,
) -> list[list[_Shot]]:
    """Split ``shots`` (already sorted by captured_at asc) into sessions.

    A new session starts whenever the gap to the previous shot exceeds
    ``gap_threshold_seconds``. The single-shot edge case is preserved:
    an isolated frame surrounded by big gaps becomes its own
    one-element session with ``duration_seconds == 0``.
    """
    if not shots:
        return []

    sessions: list[list[_Shot]] = [[shots[0]]]
    threshold = timedelta(seconds=gap_threshold_seconds)
    for previous, current in pairwise(shots):
        gap = current["captured_at"] - previous["captured_at"]
        if gap > threshold:
            sessions.append([current])
        else:
            sessions[-1].append(current)
    return sessions


def _dominant_app(shots: list[_Shot]) -> str | None:
    """Return the most-common non-NULL ``app_name`` in the block."""
    counter: Counter[str] = Counter(
        shot["app_name"] for shot in shots if shot["app_name"]
    )
    if not counter:
        return None
    return counter.most_common(1)[0][0]


def _top_titles_json(shots: list[_Shot]) -> str | None:
    """Return a JSON array of up to ``_TOP_TITLES_LIMIT`` top window titles.

    Returns ``None`` (NULL in DB) when no shot in the block had a
    title — keeps the column truly "absent" instead of an empty
    ``"[]"`` that the UI would have to special-case.
    """
    counter: Counter[str] = Counter(
        shot["window_title"] for shot in shots if shot["window_title"]
    )
    if not counter:
        return None
    titles = [title for title, _count in counter.most_common(_TOP_TITLES_LIMIT)]
    return json.dumps(titles, ensure_ascii=False)


async def _voice_seconds(
    started_at: str,
    ended_at: str,
) -> int:
    """Sum ``audio_segment.duration_seconds`` overlapping the block.

    Migration 093 renamed ``started_at`` → ``captured_at`` on
    ``audio_segment``; we read the post-rename column. We treat a
    missing table (older test DBs) as zero rather than crash so the
    worker survives partial schema states.
    """
    sql = (
        "SELECT COALESCE(SUM(duration_seconds), 0) AS total "
        "FROM audio_segment "
        "WHERE captured_at >= ? AND captured_at <= ?"
    )
    try:
        async with (
            get_connection() as conn,
            conn.execute(sql, (started_at, ended_at)) as cursor,
        ):
            row = await cursor.fetchone()
    except Exception as exc:
        log.debug("capture_sessions.voice_lookup_failed", error=str(exc))
        return 0
    if row is None:
        return 0
    total = row[0]
    return int(total or 0)


async def detect_sessions(
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
    gap_threshold_seconds: int = _DEFAULT_GAP_THRESHOLD_SECONDS,
) -> DetectStats:
    """Detect contiguous capture sessions and persist them.

    Parameters
    ----------
    lookback_hours:
        How far back from ``now()`` to scan ``screenshots``. The
        worker calls with the default; ad-hoc callers (CLI, tests) can
        widen the window to backfill historical sessions.
    gap_threshold_seconds:
        Minimum silence between adjacent shots that splits one session
        from the next. Default 1800 s (30 min).

    Returns
    -------
    :class:`DetectStats`
        ``detected`` is the number of contiguous blocks the walk
        identified; ``inserted`` is how many of those were genuinely
        new rows; ``skipped_duplicates`` is the remainder.
    """
    cutoff = (
        datetime.now(tz=UTC) - timedelta(hours=lookback_hours)
    ).isoformat()

    select_sql = (
        "SELECT id, captured_at, app_name, window_title "
        "FROM screenshots "
        "WHERE captured_at >= ? "
        "ORDER BY captured_at ASC"
    )

    async with get_connection() as conn, conn.execute(select_sql, (cutoff,)) as cursor:
        rows = await cursor.fetchall()

    shots: list[_Shot] = []
    for row in rows:
        parsed = _parse_iso(str(row["captured_at"]))
        if parsed is None:
            continue
        shots.append(
            _Shot(
                id=int(row["id"]),
                captured_at=parsed,
                app_name=row["app_name"],
                window_title=row["window_title"],
            )
        )

    sessions = _partition(shots, gap_threshold_seconds)

    insert_sql = (
        "INSERT INTO capture_session ("
        "started_at, ended_at, duration_seconds, dominant_app, "
        "screen_count, voice_seconds, top_titles_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(started_at) DO NOTHING"
    )

    inserted = 0
    detected = len(sessions)
    async with get_connection() as conn:
        for session in sessions:
            started_at = session[0]["captured_at"].isoformat()
            ended_at = session[-1]["captured_at"].isoformat()
            duration_seconds = int(
                (session[-1]["captured_at"] - session[0]["captured_at"]).total_seconds()
            )
            dominant = _dominant_app(session)
            voice = await _voice_seconds(started_at, ended_at)
            titles_json = _top_titles_json(session)
            cursor = await conn.execute(
                insert_sql,
                (
                    started_at,
                    ended_at,
                    duration_seconds,
                    dominant,
                    len(session),
                    voice,
                    titles_json,
                ),
            )
            if cursor.rowcount and cursor.rowcount > 0:
                inserted += 1
            await cursor.close()
        await conn.commit()

    skipped = detected - inserted
    log.info(
        "capture_sessions.detected",
        detected=detected,
        inserted=inserted,
        skipped_duplicates=skipped,
        lookback_hours=lookback_hours,
        gap_threshold_seconds=gap_threshold_seconds,
    )
    return DetectStats(
        detected=detected,
        inserted=inserted,
        skipped_duplicates=skipped,
    )


__all__ = [
    "DetectStats",
    "detect_sessions",
]
