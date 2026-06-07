"""Storage cleanup — drop screenshots older than the user's retention.

The cleanup is **deliberately conservative**:

  * Only deletes ``screenshots`` rows + their on-disk thumbnail files
    (everything else — notes, tags, audio transcripts, embeddings —
    is tiny and stays).
  * Honours ``shots_retention_days`` strictly. If unset → never deletes.
  * Bounded per run: deletes at most ``_MAX_PER_RUN`` rows so a runaway
    config (retention=1) doesn't blow up the worker thread.
  * Writes an audit row to ``storage_cleanup_log`` BEFORE deletion
    (with start time) and updates ``finished_at`` after, so a crashed
    worker leaves a half-row the dashboard can flag.

The dashboard at ``/storage`` calls :func:`run_cleanup` with
``trigger_source='manual'``. The background worker calls it with
``trigger_source='worker'``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

log = get_logger("persona.storage_management.cleanup")

# Tunables. These are intentionally constants (not kv-driven) so a
# misconfiguration of one user can't take the whole server down.
_MAX_PER_RUN = 5000
_MIN_RETENTION_DAYS = 1  # refuse to delete shots younger than this even
                         # if the user typed "0" by mistake

_KV_RETENTION = "shots_retention_days"
_KV_QUOTA = "shots_quota_mb"


@dataclass(slots=True, frozen=True)
class StorageSettings:
    """User-facing retention / quota policy."""

    retention_days: int | None
    quota_mb: int | None


@dataclass(slots=True, frozen=True)
class UsageBreakdown:
    """Snapshot of what's on disk right now. Counted by counting rows
    in the DB — accurate because every shot has exactly one thumbnail."""

    total_shots: int
    total_bytes: int
    oldest_captured_at: str | None
    newest_captured_at: str | None
    by_day_recent: list[dict[str, Any]]


async def get_settings() -> StorageSettings:
    """Read current policy from kv. Missing rows = no policy (no limit)."""
    async with get_connection() as conn:
        retention_raw = await get_kv(conn, _KV_RETENTION)
        quota_raw = await get_kv(conn, _KV_QUOTA)
    return StorageSettings(
        retention_days=_as_int(retention_raw),
        quota_mb=_as_int(quota_raw),
    )


async def set_settings(
    *, retention_days: int | None, quota_mb: int | None
) -> StorageSettings:
    """Upsert the policy values. ``None`` clears the row."""
    if retention_days is not None:
        retention_days = max(0, min(int(retention_days), 36_500))
        if retention_days == 0:
            retention_days = None
    if quota_mb is not None:
        quota_mb = max(0, min(int(quota_mb), 10_000_000))
        if quota_mb == 0:
            quota_mb = None
    async with get_connection() as conn:
        await set_kv(
            conn,
            _KV_RETENTION,
            "" if retention_days is None else str(retention_days),
        )
        await set_kv(
            conn,
            _KV_QUOTA,
            "" if quota_mb is None else str(quota_mb),
        )
    return await get_settings()


async def usage_breakdown(recent_days: int = 14) -> UsageBreakdown:
    """Disk usage summary. ``by_day_recent`` is the last N days, oldest-first.

    Bytes are estimated by walking the thumbnails directory for matching
    rows. We could keep a running ``bytes`` column on ``screenshots`` for
    O(1) totals, but a walk over ~30 days × ~300 shots = 9000 files is
    cheap and avoids drift if a file gets touched outside Persona.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n, "
            "       MIN(captured_at) AS oldest, "
            "       MAX(captured_at) AS newest "
            "FROM screenshots"
        )
        row = await cursor.fetchone()
        total_shots = int(row["n"] or 0)
        oldest = str(row["oldest"]) if row and row["oldest"] else None
        newest = str(row["newest"]) if row and row["newest"] else None

        # Group by date(captured_at) over recent_days. SQLite's date()
        # function does the right thing on ISO timestamps.
        cursor = await conn.execute(
            "SELECT date(captured_at) AS d, COUNT(*) AS n "
            "FROM screenshots "
            "WHERE captured_at >= date('now', ?) "
            "GROUP BY date(captured_at) "
            "ORDER BY d ASC",
            (f"-{int(recent_days)} days",),
        )
        by_day_rows = await cursor.fetchall()
        by_day = [
            {"date": str(r["d"]), "shots": int(r["n"])} for r in by_day_rows
        ]

        # Total disk usage — sum sizes of thumbnail files for all shots
        # we have. This is the expensive call, but cached at "page load"
        # frequency it's fine.
        cursor = await conn.execute(
            "SELECT thumbnail_path FROM screenshots "
            "WHERE thumbnail_path IS NOT NULL"
        )
        paths = await cursor.fetchall()

    total_bytes = 0
    for p in paths:
        try:
            total_bytes += os.path.getsize(str(p["thumbnail_path"]))
        except OSError:
            continue

    return UsageBreakdown(
        total_shots=total_shots,
        total_bytes=total_bytes,
        oldest_captured_at=oldest,
        newest_captured_at=newest,
        by_day_recent=by_day,
    )


async def run_cleanup(
    *,
    trigger_source: str = "manual",
    override_retention_days: int | None = None,
) -> dict[str, Any]:
    """Execute one cleanup pass and return a summary dict.

    ``override_retention_days`` lets the /storage page trigger a
    one-shot cleanup with a custom value without changing the saved
    policy — handy for "delete shots older than 60 days" buttons.
    """
    if trigger_source not in ("manual", "worker"):
        raise ValueError(f"unknown trigger_source: {trigger_source!r}")

    settings = await get_settings()
    retention = override_retention_days or settings.retention_days

    if retention is None or retention < _MIN_RETENTION_DAYS:
        # No policy set → no-op. We still write an audit row so the
        # dashboard shows "ran, deleted nothing because no policy".
        log_id = await _open_run(
            trigger_source=trigger_source,
            retention_days=None,
            quota_mb=settings.quota_mb,
        )
        await _close_run(log_id, shots_deleted=0, bytes_freed=0)
        return {"shots_deleted": 0, "bytes_freed": 0, "skipped": True}

    log_id = await _open_run(
        trigger_source=trigger_source,
        retention_days=retention,
        quota_mb=settings.quota_mb,
    )

    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT id, thumbnail_path FROM screenshots "
                "WHERE captured_at < datetime('now', ?) "
                "ORDER BY id ASC LIMIT ?",
                (f"-{int(retention)} days", _MAX_PER_RUN),
            )
            victims = await cursor.fetchall()

        bytes_freed = 0
        ids_deleted: list[int] = []
        for row in victims:
            path = row["thumbnail_path"]
            shot_id = int(row["id"])
            if path:
                try:
                    bytes_freed += os.path.getsize(str(path))
                    Path(str(path)).unlink(missing_ok=True)
                except OSError as exc:
                    log.warning(
                        "cleanup.unlink_failed", shot_id=shot_id, error=str(exc)
                    )
            ids_deleted.append(shot_id)

        if ids_deleted:
            async with get_connection() as conn:
                placeholders = ",".join("?" * len(ids_deleted))
                await conn.execute(
                    f"DELETE FROM screenshots WHERE id IN ({placeholders})",
                    ids_deleted,
                )
                await conn.commit()

        await _close_run(
            log_id, shots_deleted=len(ids_deleted), bytes_freed=bytes_freed
        )
        log.info(
            "cleanup.completed",
            trigger_source=trigger_source,
            shots_deleted=len(ids_deleted),
            bytes_freed=bytes_freed,
        )
        return {
            "shots_deleted": len(ids_deleted),
            "bytes_freed": bytes_freed,
            "skipped": False,
        }
    except Exception as exc:
        await _close_run(log_id, shots_deleted=0, bytes_freed=0, error=str(exc))
        log.error("cleanup.failed", error=str(exc))
        raise


async def list_cleanup_runs(limit: int = 20) -> list[dict[str, Any]]:
    """Audit-log rows, newest first."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM storage_cleanup_log "
            "ORDER BY started_at DESC LIMIT ?",
            (max(1, min(int(limit), 200)),),
        )
        rows = await cursor.fetchall()
    return [
        {
            "id": int(r["id"]),
            "started_at": str(r["started_at"]),
            "finished_at": (
                str(r["finished_at"]) if r["finished_at"] is not None else None
            ),
            "trigger_source": str(r["trigger_source"]),
            "shots_deleted": int(r["shots_deleted"] or 0),
            "bytes_freed": int(r["bytes_freed"] or 0),
            "error": str(r["error"]) if r["error"] is not None else None,
            "retention_days": (
                int(r["retention_days"]) if r["retention_days"] is not None else None
            ),
            "quota_mb": (
                int(r["quota_mb"]) if r["quota_mb"] is not None else None
            ),
        }
        for r in rows
    ]


# --- Internal helpers ------------------------------------------------------


def _as_int(raw: str | None) -> int | None:
    if not raw:
        return None
    try:
        n = int(str(raw).strip())
        return n if n > 0 else None
    except ValueError:
        return None


async def _open_run(
    *,
    trigger_source: str,
    retention_days: int | None,
    quota_mb: int | None,
) -> int:
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO storage_cleanup_log "
            "  (trigger_source, retention_days, quota_mb) "
            "VALUES (?, ?, ?)",
            (trigger_source, retention_days, quota_mb),
        )
        await conn.commit()
        return int(cursor.lastrowid or 0)


async def _close_run(
    log_id: int,
    *,
    shots_deleted: int,
    bytes_freed: int,
    error: str | None = None,
) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE storage_cleanup_log SET "
            "  finished_at = datetime('now'), "
            "  shots_deleted = ?, "
            "  bytes_freed = ?, "
            "  error = ? "
            "WHERE id = ?",
            (shots_deleted, bytes_freed, error, log_id),
        )
        await conn.commit()
