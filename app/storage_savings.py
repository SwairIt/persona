"""Per-day storage-savings journal — record + read.

Three housekeeping passes quietly trim Persona's disk footprint and the
operator never sees a cumulative timeline of how much they reclaimed:

* :func:`app.dedup.phash.find_or_create_dedup_group` skips writing a
  screenshot whose perceptual hash matches a recent group — the bytes
  saved are the would-be footprint of that screenshot.
* :func:`app.thumb_dedup.scan_and_dedup` collapses byte-identical
  thumbnail files onto a single canonical copy — the bytes saved are the
  real reclaimed bytes from the duplicate files it unlinked.
* :func:`app.recycle.purge_expired` hard-deletes soft-deleted screenshots
  whose retention window has expired — the bytes saved are the on-disk
  size of the thumbnail files it unlinked.

Each pass calls one of :func:`record_dedup_hit`, :func:`record_thumb_dedup`,
or :func:`record_retention_freed` to upsert the running total into a
single ``storage_saving`` row keyed by the current UTC day. The
``/stats/storage-savings`` page reads that journal via :func:`chart_data`
and renders a 30-day line chart plus a per-day breakdown table.

The recorders are best-effort: any database failure is logged and
swallowed so a housekeeping pass never aborts because the savings
journal couldn't be updated.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.savings")


class DailySaving(TypedDict):
    """One day's storage savings, oldest-first when listed by :func:`chart_data`.

    * ``day`` — ``YYYY-MM-DD`` (UTC).
    * ``bytes_saved`` — rolling total across all three recorders.
    * ``dedup_hits`` — count of duplicates the pHash pass skipped.
    * ``thumb_dedup_bytes`` — bytes reclaimed by the on-disk thumb-dedup pass.
    * ``retention_freed_bytes`` — bytes reclaimed by hard-deleting expired
      recycle-bin entries.
    """

    day: str
    bytes_saved: int
    dedup_hits: int
    thumb_dedup_bytes: int
    retention_freed_bytes: int


def _today_utc() -> str:
    """Return the current UTC day as ``YYYY-MM-DD``.

    Wrapped in a helper so the recorders share one definition of "today"
    — important when the chart and the writer disagree on the day
    boundary (e.g. midnight UTC straddle).
    """
    return datetime.now(UTC).date().isoformat()


async def record_dedup_hit(bytes_saved: int) -> None:
    """Credit one pHash dedup hit to today's savings row.

    The dedup pass operates pre-write: there is no on-disk file to
    measure, only the would-be footprint of the skipped screenshot. The
    caller supplies an estimated ``bytes_saved`` (typical thumbnail JPEG
    size in production); this function bumps both the rolling total and
    the dedup-hit counter so the chart can credit the bytes and the
    table can credit the count.

    Negative values are clamped to zero so a misbehaving caller cannot
    drive the rolling total downwards.
    """
    safe_bytes = max(0, int(bytes_saved))
    await _upsert_today(
        bytes_delta=safe_bytes,
        dedup_hits_delta=1,
        thumb_dedup_bytes_delta=0,
        retention_freed_bytes_delta=0,
        source="dedup_hit",
    )


async def record_thumb_dedup(bytes_freed: int) -> None:
    """Credit a thumbnail-dedup batch to today's savings row.

    Called once per :func:`app.thumb_dedup.scan_and_dedup` invocation
    with the real ``bytes_freed`` total the batch reclaimed by unlinking
    duplicate JPEG files. A zero-byte batch (no duplicates found) is a
    no-op so we don't pollute the journal with empty bumps.

    Negative values are clamped to zero — a stat()/unlink() race could
    theoretically produce a negative delta and we never want the
    rolling total to regress.
    """
    safe_bytes = max(0, int(bytes_freed))
    if safe_bytes == 0:
        return
    await _upsert_today(
        bytes_delta=safe_bytes,
        dedup_hits_delta=0,
        thumb_dedup_bytes_delta=safe_bytes,
        retention_freed_bytes_delta=0,
        source="thumb_dedup",
    )


async def record_retention_freed(bytes_freed: int) -> None:
    """Credit a recycle-bin purge to today's savings row.

    Called once per :func:`app.recycle.purge_expired` invocation with
    the total on-disk bytes the purge reclaimed by unlinking expired
    thumbnails. A zero-byte purge (nothing expired or nothing on disk)
    is a no-op so we don't pollute the journal with empty bumps.

    Negative values are clamped to zero — same invariant as
    :func:`record_thumb_dedup`.
    """
    safe_bytes = max(0, int(bytes_freed))
    if safe_bytes == 0:
        return
    await _upsert_today(
        bytes_delta=safe_bytes,
        dedup_hits_delta=0,
        thumb_dedup_bytes_delta=0,
        retention_freed_bytes_delta=safe_bytes,
        source="retention_freed",
    )


async def _upsert_today(
    *,
    bytes_delta: int,
    dedup_hits_delta: int,
    thumb_dedup_bytes_delta: int,
    retention_freed_bytes_delta: int,
    source: str,
) -> None:
    """Insert-or-bump today's ``storage_saving`` row.

    Uses ``INSERT ... ON CONFLICT(day) DO UPDATE`` so a single round-trip
    handles both the "first record of the day" and the "already-recorded
    today" cases atomically. The recorders are best-effort: any database
    failure (locked, schema missing in a half-initialised test DB) is
    logged and swallowed so housekeeping never aborts because the journal
    couldn't be updated.
    """
    today = _today_utc()
    try:
        async with get_connection() as conn:
            await conn.execute(
                """
                INSERT INTO storage_saving (
                    day,
                    bytes_saved,
                    dedup_hits,
                    thumb_dedup_bytes,
                    retention_freed_bytes
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    bytes_saved = bytes_saved + excluded.bytes_saved,
                    dedup_hits = dedup_hits + excluded.dedup_hits,
                    thumb_dedup_bytes = thumb_dedup_bytes + excluded.thumb_dedup_bytes,
                    retention_freed_bytes =
                        retention_freed_bytes + excluded.retention_freed_bytes
                """,
                (
                    today,
                    bytes_delta,
                    dedup_hits_delta,
                    thumb_dedup_bytes_delta,
                    retention_freed_bytes_delta,
                ),
            )
            await conn.commit()
    except Exception as exc:
        log.warning(
            "savings.record_failed",
            source=source,
            day=today,
            bytes_delta=bytes_delta,
            error=str(exc),
        )
        return

    log.debug(
        "savings.recorded",
        source=source,
        day=today,
        bytes_delta=bytes_delta,
        dedup_hits_delta=dedup_hits_delta,
        thumb_dedup_bytes_delta=thumb_dedup_bytes_delta,
        retention_freed_bytes_delta=retention_freed_bytes_delta,
    )


async def chart_data(days: int = 30) -> list[DailySaving]:
    """Return the last ``days`` of savings, oldest first.

    Days with no recorded savings are filled in as zeros so the chart's
    x-axis stays uniform — a sparse journal must not produce a jagged
    line. ``days`` is clamped to ``[1, 365]`` so a misconfigured query
    string cannot ask for unbounded history.
    """
    capped = max(1, min(int(days), 365))

    today = datetime.now(UTC).date()
    start_day = today - timedelta(days=capped - 1)
    start_iso = start_day.isoformat()

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT day,
                   bytes_saved,
                   dedup_hits,
                   thumb_dedup_bytes,
                   retention_freed_bytes
            FROM storage_saving
            WHERE day >= ?
            ORDER BY day
            """,
            (start_iso,),
        )
        rows = await cursor.fetchall()

    by_day: dict[str, DailySaving] = {
        str(row["day"]): DailySaving(
            day=str(row["day"]),
            bytes_saved=int(row["bytes_saved"]),
            dedup_hits=int(row["dedup_hits"]),
            thumb_dedup_bytes=int(row["thumb_dedup_bytes"]),
            retention_freed_bytes=int(row["retention_freed_bytes"]),
        )
        for row in rows
    }

    out: list[DailySaving] = []
    cursor_day: date = start_day
    while cursor_day <= today:
        key = cursor_day.isoformat()
        out.append(
            by_day.get(
                key,
                DailySaving(
                    day=key,
                    bytes_saved=0,
                    dedup_hits=0,
                    thumb_dedup_bytes=0,
                    retention_freed_bytes=0,
                ),
            )
        )
        cursor_day += timedelta(days=1)

    return out


__all__ = [
    "DailySaving",
    "chart_data",
    "record_dedup_hit",
    "record_retention_freed",
    "record_thumb_dedup",
]
