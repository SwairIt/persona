"""Audio segment merger — collapse VAD-fragmented utterances (v1.40).

Silero-VAD sometimes splits a continuous voice run into 2-3 short
segments separated by sub-second silences. The DB ends up with
several tiny rows where a human would write one. This module exposes
the logical-merge primitives used by
:mod:`app.workers.audio_merge_worker`.

Design notes
------------
* **Logical merge only.** We never re-encode or concatenate audio
  files on disk — the new ``audio_segment`` row reuses the *first*
  fragment's ``path`` so the user can still seek to the recording.
  A future feature can wire up a real ffmpeg concat; the columns
  added by migration 123 are forward-compatible with that.
* **Idempotent.** ``find_merge_candidates`` excludes rows whose
  ``merged_into_id`` is already set, and ``merge_group`` is a single
  transaction — a crashed run leaves the database in a consistent
  state (either everything merged or nothing).
* **Conservative grouping.** A group only forms when EVERY adjacent
  pair has a silence gap strictly below ``gap_seconds``. The default
  ``1.0 s`` matches the "natural pause" upper bound observed in the
  capture loop's VAD logs; bumping it would start merging
  semantically distinct utterances.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.audio_segment_merge")


_DEFAULT_GAP_SECONDS: Final[float] = 1.0
"""Maximum silence (seconds) between two fragments that still merges."""

_DEFAULT_LOOKBACK_HOURS: Final[int] = 24
"""How far back ``find_merge_candidates`` scans by default."""


class MergeResult(TypedDict):
    """Return shape of :func:`merge_group`."""

    merged_into_id: int
    count_merged: int
    total_duration_seconds: float


class MergeStats(TypedDict):
    """Return shape of :func:`stats_merge`."""

    merged_into_count: int
    """Rows whose ``merged_into_id`` is non-NULL (the absorbed fragments)."""

    unmerged_count: int
    """Rows whose ``merged_into_id`` is NULL (canonical / standalone)."""

    total: int
    """Convenience sum of the two above."""


def _parse_iso(value: str | None) -> datetime | None:
    """Parse ISO-8601 timestamps tolerantly (naive → UTC).

    Audio rows are written with either ``YYYY-MM-DDTHH:MM:SS`` (naive
    UTC, the legacy capture-loop default) or ``...+00:00`` (current).
    Anything unparseable is logged at debug and treated as missing so
    one bad row can't break the whole grouping pass.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        log.debug("audio_segment_merge.bad_timestamp", value=value[:64])
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


async def find_merge_candidates(
    gap_seconds: float = _DEFAULT_GAP_SECONDS,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
) -> list[list[int]]:
    """Group adjacent ``audio_segment`` rows separated by < ``gap_seconds``.

    Args:
        gap_seconds: Maximum inter-segment silence (in seconds) for two
            rows to belong to the same group. Strictly-less-than, so a
            gap of exactly ``gap_seconds`` does NOT merge — keeps the
            default ``1.0 s`` behaviour intuitive ("less than a second").
        lookback_hours: How far back to scan. The worker re-runs every
            30 minutes, so a 24 h window comfortably covers any backlog
            without scanning the entire table.

    Returns:
        A list of groups; each group is a list of ``audio_segment.id``
        values in chronological order, length ≥ 2. Single-row "groups"
        are filtered out — there is nothing to merge.
    """
    if gap_seconds <= 0:
        return []
    if lookback_hours <= 0:
        return []

    window_start = datetime.now(tz=UTC).timestamp() - lookback_hours * 3600
    window_start_iso = datetime.fromtimestamp(window_start, tz=UTC).isoformat()

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, captured_at, ended_at "
            "FROM audio_segment "
            "WHERE merged_into_id IS NULL "
            "  AND captured_at >= ? "
            "ORDER BY captured_at ASC, id ASC",
            (window_start_iso,),
        )
        rows = list(await cursor.fetchall())

    groups: list[list[int]] = []
    current: list[int] = []
    prev_end: datetime | None = None

    for row in rows:
        seg_id = int(row["id"])
        captured = _parse_iso(row["captured_at"])
        ended = _parse_iso(row["ended_at"])
        if captured is None or ended is None:
            # Bad timestamp — flush the open group; this row can't
            # participate without reliable bounds.
            if len(current) >= 2:
                groups.append(current)
            current = []
            prev_end = None
            continue

        if prev_end is None:
            current = [seg_id]
            prev_end = ended
            continue

        gap = (captured - prev_end).total_seconds()
        if gap < gap_seconds:
            current.append(seg_id)
            prev_end = ended
        else:
            if len(current) >= 2:
                groups.append(current)
            current = [seg_id]
            prev_end = ended

    if len(current) >= 2:
        groups.append(current)

    log.info(
        "audio_segment_merge.candidates",
        gap_seconds=gap_seconds,
        lookback_hours=lookback_hours,
        scanned_rows=len(rows),
        groups=len(groups),
    )
    return groups


async def merge_group(segment_ids: list[int]) -> MergeResult:
    """Logically merge ``segment_ids`` into one new canonical row.

    The new ``audio_segment`` row carries:
      * ``captured_at`` from the FIRST fragment,
      * ``ended_at`` from the LAST fragment,
      * ``duration_seconds`` = sum of the fragments' durations
        (NOT ``ended_at - captured_at`` — the silence gap is
        deliberately excluded so downstream "voice minutes" reports
        stay accurate),
      * concatenated ``transcript`` joined by single spaces (NULL
        fragments are dropped; an all-NULL group yields a NULL
        transcript),
      * ``path`` / ``codec`` / ``bitrate`` / ``size_bytes`` /
        ``locale`` copied from the FIRST fragment (the file on disk
        is not actually concatenated — see module docstring).

    The original fragments are then UPDATEd with
    ``merged_into_id = <new id>`` + ``merged_at = <now ISO>``. The
    whole operation is one transaction.

    Raises:
        ValueError: if fewer than 2 ids are supplied (nothing to
            merge) or any id is missing / already merged.
    """
    if len(segment_ids) < 2:
        raise ValueError("merge_group requires at least 2 segment ids")

    placeholders = ",".join(["?"] * len(segment_ids))
    sql_select = (
        f"SELECT id, captured_at, ended_at, duration_seconds, codec, bitrate, "  # noqa: S608 — placeholders are integers we built ourselves
        f"       path, size_bytes, transcript, locale, merged_into_id "
        f"FROM audio_segment WHERE id IN ({placeholders}) "
        f"ORDER BY captured_at ASC, id ASC"
    )

    async with get_connection() as conn:
        cursor = await conn.execute(sql_select, tuple(segment_ids))
        rows = list(await cursor.fetchall())
        if len(rows) != len(segment_ids):
            raise ValueError(
                f"merge_group: expected {len(segment_ids)} rows, got {len(rows)}"
            )
        for row in rows:
            if row["merged_into_id"] is not None:
                raise ValueError(
                    f"merge_group: segment id={int(row['id'])} already merged"
                )

        first = rows[0]
        last = rows[-1]

        total_duration = float(sum(float(r["duration_seconds"] or 0.0) for r in rows))
        transcript_parts = [
            str(r["transcript"]).strip()
            for r in rows
            if r["transcript"] is not None and str(r["transcript"]).strip()
        ]
        combined_transcript: str | None = (
            " ".join(transcript_parts) if transcript_parts else None
        )

        bitrate_value = first["bitrate"]
        bitrate: int | None = int(bitrate_value) if bitrate_value is not None else None
        locale_value = first["locale"]
        locale: str | None = str(locale_value) if locale_value is not None else None

        # Sum of source sizes is a more honest "bytes on disk" estimate
        # than copying the first fragment's size — even though the new
        # row points at the first fragment's file, the original files
        # all still exist on disk until retention reaps them.
        total_size = int(sum(int(r["size_bytes"] or 0) for r in rows))

        insert_cursor = await conn.execute(
            "INSERT INTO audio_segment "
            "(captured_at, ended_at, duration_seconds, codec, bitrate, "
            " path, size_bytes, transcript, locale) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(first["captured_at"]),
                str(last["ended_at"]),
                total_duration,
                str(first["codec"]),
                bitrate,
                str(first["path"]),
                total_size,
                combined_transcript,
                locale,
            ),
        )
        new_id = int(insert_cursor.lastrowid or 0)
        if new_id <= 0:
            raise RuntimeError("merge_group: SQLite returned no lastrowid")

        merged_at_iso = datetime.now(tz=UTC).isoformat()
        sql_update = (
            f"UPDATE audio_segment "  # noqa: S608 — placeholders are integers we built ourselves
            f"SET merged_into_id = ?, merged_at = ? "
            f"WHERE id IN ({placeholders})"
        )
        await conn.execute(sql_update, (new_id, merged_at_iso, *segment_ids))
        await conn.commit()

    log.info(
        "audio_segment_merge.merged",
        merged_into_id=new_id,
        count_merged=len(segment_ids),
        total_duration_seconds=total_duration,
    )
    result: MergeResult = {
        "merged_into_id": new_id,
        "count_merged": len(segment_ids),
        "total_duration_seconds": total_duration,
    }
    return result


async def stats_merge() -> MergeStats:
    """Count merged-into vs unmerged ``audio_segment`` rows."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT "
            "  SUM(CASE WHEN merged_into_id IS NOT NULL THEN 1 ELSE 0 END) "
            "    AS merged_into_count, "
            "  SUM(CASE WHEN merged_into_id IS NULL THEN 1 ELSE 0 END) "
            "    AS unmerged_count "
            "FROM audio_segment"
        )
        row = await cursor.fetchone()

    merged_into_count = int(row["merged_into_count"] or 0) if row is not None else 0
    unmerged_count = int(row["unmerged_count"] or 0) if row is not None else 0
    stats: MergeStats = {
        "merged_into_count": merged_into_count,
        "unmerged_count": unmerged_count,
        "total": merged_into_count + unmerged_count,
    }
    return stats


__all__ = [
    "MergeResult",
    "MergeStats",
    "find_merge_candidates",
    "merge_group",
    "stats_merge",
]
