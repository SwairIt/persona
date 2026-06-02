"""Dry-run preview for the retention worker — v0.45.

The retention worker (:mod:`app.workers.retention`) demotes screenshots
hot -> warm -> cold based on ``settings.tier_warm_after_days`` and
``settings.tier_cold_after_days`` and, separately, hard-deletes any
``recycle_bin`` row older than ``settings.recycle_retention_days`` via
:func:`app.recycle.purge_expired`.

This module asks the same questions against the same tables WITHOUT
mutating anything: it counts how many rows would be touched on the next
run, samples a few thumbnails for the UI, and adds up the on-disk size
of those thumbnails so the user can decide whether the retention
settings are doing what they want before flipping them.

Counts are returned as a plain ``dict`` so the JSON route can dump it
directly with no serialisation glue.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TypedDict

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.time import iso, parse_iso

log = get_logger("persona.retention.preview")

# How many sample IDs to surface per bucket. Kept small so the page can
# afford to render an actual thumbnail per ID without paginating.
_SAMPLE_LIMIT = 6


class _BucketSamples(TypedDict):
    """Sample screenshot IDs per retention bucket."""

    warm: list[int]
    cold: list[int]
    delete: list[int]


class RetentionPreview(TypedDict):
    """Shape returned by :func:`preview`."""

    to_demote_warm: int
    to_demote_cold: int
    to_hard_delete: int
    sample_ids: _BucketSamples
    total_bytes_freed_estimate: int


def _resolve_now(now_iso: str | None) -> datetime:
    """Parse the override timestamp, defaulting to ``datetime.now(UTC)``.

    Accepts the same ISO 8601 strings :func:`app.storage.time.iso`
    produces. Naive datetimes are coerced to UTC so the SQL comparison
    against ``captured_at`` always uses the same tz the worker uses.
    """
    if now_iso is None:
        return datetime.now(UTC)
    parsed = parse_iso(now_iso)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _thumb_bytes(thumbnail_path: str | None) -> int:
    """Return the on-disk size of ``thumbnail_path`` or ``0`` if missing.

    The retention worker frees these bytes when it demotes a row to
    cold (and again, indirectly, when it purges a recycle-bin row whose
    thumbnail still exists). We sum them across all candidates to give
    the user a meaningful "X MB will be freed" figure.
    """
    if not thumbnail_path:
        return 0
    try:
        path = Path(thumbnail_path)
        if not path.exists():
            return 0
        return path.stat().st_size
    except OSError as exc:  # pragma: no cover — best-effort accounting
        log.warning(
            "retention.preview.stat_failed",
            path=thumbnail_path,
            error=str(exc),
        )
        return 0


async def preview(now_iso: str | None = None) -> RetentionPreview:
    """Return what the retention worker WOULD touch on its next run.

    Mirrors the SQL in :mod:`app.workers.retention` (``_demote_to_warm``,
    ``_demote_to_cold``) and :func:`app.recycle.purge_expired` exactly so
    the numbers match the next sweep — no writes happen here.

    ``now_iso`` lets tests pin "now" deterministically; production callers
    pass nothing and the function falls back to the real clock.
    """
    settings = get_settings()
    now = _resolve_now(now_iso)
    warm_cutoff = now - timedelta(days=settings.tier_warm_after_days)
    cold_cutoff = now - timedelta(days=settings.tier_cold_after_days)
    recycle_cutoff = now - timedelta(days=settings.recycle_retention_days)
    recycle_cutoff_str = recycle_cutoff.strftime("%Y-%m-%d %H:%M:%S")

    warm_cutoff_iso = iso(warm_cutoff)
    cold_cutoff_iso = iso(cold_cutoff)

    async with get_connection() as conn:
        warm_count_cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots "
            "WHERE tier = 'hot' AND captured_at < ? "
            "AND thumbnail_path IS NOT NULL",
            (warm_cutoff_iso,),
        )
        warm_count_row = await warm_count_cursor.fetchone()
        warm_count = int(warm_count_row["n"]) if warm_count_row else 0

        warm_sample_cursor = await conn.execute(
            "SELECT id, thumbnail_path FROM screenshots "
            "WHERE tier = 'hot' AND captured_at < ? "
            "AND thumbnail_path IS NOT NULL "
            "ORDER BY captured_at ASC LIMIT ?",
            (warm_cutoff_iso, _SAMPLE_LIMIT),
        )
        warm_sample_rows = await warm_sample_cursor.fetchall()

        cold_count_cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM screenshots "
            "WHERE tier IN ('hot', 'warm') AND captured_at < ?",
            (cold_cutoff_iso,),
        )
        cold_count_row = await cold_count_cursor.fetchone()
        cold_count = int(cold_count_row["n"]) if cold_count_row else 0

        cold_sample_cursor = await conn.execute(
            "SELECT id, thumbnail_path FROM screenshots "
            "WHERE tier IN ('hot', 'warm') AND captured_at < ? "
            "ORDER BY captured_at ASC LIMIT ?",
            (cold_cutoff_iso, _SAMPLE_LIMIT),
        )
        cold_sample_rows = await cold_sample_cursor.fetchall()

        # cold-bytes estimate — sum the actual file sizes of the
        # thumbnails the worker would unlink. We pull ALL candidate paths
        # (not just the sample) so the figure is meaningful even when the
        # backlog is large.
        cold_all_cursor = await conn.execute(
            "SELECT thumbnail_path FROM screenshots "
            "WHERE tier IN ('hot', 'warm') AND captured_at < ? "
            "AND thumbnail_path IS NOT NULL",
            (cold_cutoff_iso,),
        )
        cold_all_rows = await cold_all_cursor.fetchall()

        delete_count_cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM recycle_bin WHERE deleted_at < ?",
            (recycle_cutoff_str,),
        )
        delete_count_row = await delete_count_cursor.fetchone()
        delete_count = int(delete_count_row["n"]) if delete_count_row else 0

        delete_sample_cursor = await conn.execute(
            "SELECT id, thumbnail_path FROM recycle_bin "
            "WHERE deleted_at < ? ORDER BY deleted_at ASC LIMIT ?",
            (recycle_cutoff_str, _SAMPLE_LIMIT),
        )
        delete_sample_rows = await delete_sample_cursor.fetchall()

        delete_all_cursor = await conn.execute(
            "SELECT thumbnail_path FROM recycle_bin "
            "WHERE deleted_at < ? AND thumbnail_path IS NOT NULL",
            (recycle_cutoff_str,),
        )
        delete_all_rows = await delete_all_cursor.fetchall()

    bytes_estimate = 0
    for row in cold_all_rows:
        bytes_estimate += _thumb_bytes(
            str(row["thumbnail_path"]) if row["thumbnail_path"] else None,
        )
    for row in delete_all_rows:
        bytes_estimate += _thumb_bytes(
            str(row["thumbnail_path"]) if row["thumbnail_path"] else None,
        )

    result: RetentionPreview = {
        "to_demote_warm": warm_count,
        "to_demote_cold": cold_count,
        "to_hard_delete": delete_count,
        "sample_ids": {
            "warm": [int(r["id"]) for r in warm_sample_rows],
            "cold": [int(r["id"]) for r in cold_sample_rows],
            "delete": [int(r["id"]) for r in delete_sample_rows],
        },
        "total_bytes_freed_estimate": bytes_estimate,
    }

    log.info(
        "retention.preview.computed",
        to_demote_warm=warm_count,
        to_demote_cold=cold_count,
        to_hard_delete=delete_count,
        total_bytes_freed_estimate=bytes_estimate,
    )
    return result


__all__ = ["RetentionPreview", "preview"]
