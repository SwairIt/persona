"""Audit-log rotation + gzip archive (v1.48).

The ``audit_log`` table (migration 037) accumulates one row per
destructive admin action and has no built-in pruning. On a multi-month
install the table grows past tens of thousands of rows and the
``idx_audit_log_ts`` index — the one the security review UI uses to
list "what changed today" — starts to feel sluggish.

This module is the one-shot core that the daily worker
(:mod:`app.workers.audit_log_rotation_worker`) and the "Run Now" route
(:mod:`app.web.routes.audit_log_rotation`) both call.

Algorithm
---------
1. ``SELECT COUNT(*) FROM audit_log``.
2. If ``count <= keep_rows``: return ``{"status": "not_needed", ...}``.
3. Otherwise read the oldest ``count - keep_rows`` rows ordered by
   ``id ASC`` (id is monotonic AUTOINCREMENT and tracks insertion
   order, so this is also chronological — and stable when two rows
   share the same ``ts``).
4. Stream those rows into a gzipped JSONL file at
   ``<archive_dir>/audit-YYYY-MM-DD-HHMM.jsonl.gz`` (UTC, minute
   resolution — collisions within the same minute are impractical
   because the worker fires once per day, but the route's "Run Now"
   button could theoretically be mashed; on a collision we suffix
   ``-N`` so we never overwrite an existing archive).
5. ``DELETE FROM audit_log WHERE id <= :max_id`` — single statement,
   single round-trip. We use the **maximum archived id**, not a
   timestamp comparison, because two rows can share ``ts`` at the
   second granularity SQLite produces by default and ``id`` is the
   only key that guarantees we delete exactly what we archived.
6. Insert one row into ``audit_log_archive_run`` for the UI.

Defaults
--------
* ``keep_rows = 5000`` — empirically ~3 months of rows on a single-user
  install with moderate admin churn. Tunable via the function arg or
  (via the worker) the ``audit_log_rotation_keep_rows`` kv setting.
* ``archive_dir = settings.data_dir / "audit-archives"`` — under
  ``~/.persona/`` by default; the directory is created on demand.

Safety
------
* All SQL is parametrised.
* The DELETE is performed in the SAME transaction as nothing else —
  we COMMIT explicitly after the delete so a crash leaves either the
  full archive + cleared rows, or the archive with the rows still in
  the DB (a subsequent run will re-archive those rows into a *new*
  file; the duplicate is preserved for forensics and the operator can
  reconcile from ``id`` + ``ts`` if it ever matters). We deliberately
  do NOT delete first / archive second; losing rows on disk-full is
  worse than a duplicated archive entry.
* The gzip file is fsynced before the DELETE so a power loss between
  "wrote archive" and "deleted rows" still has the archive on disk.
"""

from __future__ import annotations

import gzip
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict

from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection

log = get_logger("persona.audit_log_rotation")


class RotationResult(TypedDict, total=False):
    """Return shape of :func:`rotate_audit_log`.

    ``status`` is always present. The other fields are present only
    when ``status == "ok"``.
    """

    status: str
    count: int
    rows_archived: int
    file_path: str
    file_size_bytes: int


_DEFAULT_KEEP_ROWS: int = 5000
_ARCHIVE_SUBDIR: str = "audit-archives"
_FILENAME_PREFIX: str = "audit-"
_FILENAME_SUFFIX: str = ".jsonl.gz"


async def rotate_audit_log(
    keep_rows: int = _DEFAULT_KEEP_ROWS,
    archive_dir: str | None = None,
) -> RotationResult:
    """Archive the oldest excess rows of ``audit_log`` into a gzipped JSONL.

    Parameters
    ----------
    keep_rows:
        Target row count to leave in the live ``audit_log`` table.
        Clamped to ``max(0, keep_rows)``; a value of ``0`` means
        "archive everything".
    archive_dir:
        Directory the gzipped JSONL is written to. ``None`` (default)
        resolves to ``settings.data_dir / "audit-archives"``.

    Returns
    -------
    dict
        ``{"status": "not_needed", "count": int}`` when the table is at
        or below the keep-rows threshold.
        ``{"status": "ok", "rows_archived": int, "file_path": str,
        "file_size_bytes": int}`` otherwise.
    """
    keep = max(0, int(keep_rows))
    target_dir = _resolve_archive_dir(archive_dir)

    async with get_connection() as conn:
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM audit_log")
        count_row = await cursor.fetchone()
        total = int(count_row["n"]) if count_row else 0

        if total <= keep:
            log.info(
                "audit_log_rotation.not_needed", count=total, keep_rows=keep,
            )
            return {"status": "not_needed", "count": total}

        excess = total - keep
        # ``id`` is AUTOINCREMENT and monotonic so ORDER BY id ASC is
        # the chronological order; using ``id`` (not ``ts``) avoids the
        # ambiguity of two rows sharing a second-granularity timestamp.
        cursor = await conn.execute(
            "SELECT id, ts, action, actor, target, detail, success "
            "FROM audit_log ORDER BY id ASC LIMIT ?",
            (excess,),
        )
        rows = await cursor.fetchall()

    if not rows:
        # Race: another rotator emptied the table between our COUNT and
        # SELECT. Treat as a no-op rather than crash.
        log.info("audit_log_rotation.race_empty", count=total)
        return {"status": "not_needed", "count": 0}

    serialised: list[dict[str, Any]] = [_row_to_dict(row) for row in rows]
    max_archived_id: int = max(int(r["id"]) for r in serialised)
    oldest_ts: str | None = serialised[0]["ts"]
    newest_ts: str | None = serialised[-1]["ts"]
    rows_archived = len(serialised)

    target_dir.mkdir(parents=True, exist_ok=True)
    file_path = _unique_archive_path(target_dir)
    _write_jsonl_gz(file_path, serialised)
    file_size = file_path.stat().st_size

    # Delete the archived rows + record the run in a single connection
    # session so the bookkeeping row and the delete commit atomically.
    async with get_connection() as conn:
        await conn.execute(
            "DELETE FROM audit_log WHERE id <= ?", (max_archived_id,),
        )
        await conn.execute(
            "INSERT INTO audit_log_archive_run "
            "(oldest_row_at, newest_row_at, rows_archived, file_path, "
            "file_size_bytes) VALUES (?, ?, ?, ?, ?)",
            (
                oldest_ts,
                newest_ts,
                rows_archived,
                str(file_path),
                int(file_size),
            ),
        )
        await conn.commit()

    log.info(
        "audit_log_rotation.ok",
        rows_archived=rows_archived,
        file_path=str(file_path),
        file_size_bytes=int(file_size),
        oldest_ts=oldest_ts,
        newest_ts=newest_ts,
    )
    return {
        "status": "ok",
        "rows_archived": rows_archived,
        "file_path": str(file_path),
        "file_size_bytes": int(file_size),
    }


def _resolve_archive_dir(archive_dir: str | None) -> Path:
    """Return the absolute :class:`Path` archives should land under."""
    if archive_dir:
        return Path(archive_dir).expanduser().resolve()
    settings = get_settings()
    return (settings.data_dir / _ARCHIVE_SUBDIR).resolve()


def _unique_archive_path(target_dir: Path) -> Path:
    """Pick a filename inside ``target_dir`` that does not yet exist.

    Base shape is ``audit-YYYY-MM-DD-HHMM.jsonl.gz`` (UTC, minute
    resolution). The worker fires once per day so collisions are only
    realistic when the operator mashes the "Run Now" button; we suffix
    ``-N`` until we find a free slot rather than overwriting.
    """
    now = datetime.now(UTC).strftime("%Y-%m-%d-%H%M")
    candidate = target_dir / f"{_FILENAME_PREFIX}{now}{_FILENAME_SUFFIX}"
    if not candidate.exists():
        return candidate
    counter = 1
    while True:
        candidate = (
            target_dir
            / f"{_FILENAME_PREFIX}{now}-{counter}{_FILENAME_SUFFIX}"
        )
        if not candidate.exists():
            return candidate
        counter += 1


def _write_jsonl_gz(path: Path, rows: list[dict[str, Any]]) -> None:
    """Stream ``rows`` as one JSON object per line into a gzipped file.

    fsynced before close so a power loss between "wrote archive" and
    the subsequent DELETE still leaves the archive on disk.
    """
    with gzip.open(path, "wt", encoding="utf-8", newline="\n") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
        fh.flush()
        try:
            os.fsync(fh.fileno())
        except (OSError, AttributeError):
            # fsync is best-effort: on Windows / on a gzip wrapper that
            # hides the fd we silently fall back to the OS's own buffer
            # flush. The DELETE-after-archive ordering keeps us safe
            # even without fsync — we just lose the strict guarantee.
            log.debug("audit_log_rotation.fsync_skipped", path=str(path))


def _row_to_dict(row: Any) -> dict[str, Any]:
    """Convert one aiosqlite Row of ``audit_log`` to a JSONL-safe dict."""
    return {
        "id": int(row["id"]),
        "ts": str(row["ts"]) if row["ts"] is not None else None,
        "action": str(row["action"]) if row["action"] is not None else None,
        "actor": str(row["actor"]) if row["actor"] is not None else None,
        "target": str(row["target"]) if row["target"] is not None else None,
        "detail": str(row["detail"]) if row["detail"] is not None else None,
        "success": int(row["success"]) if row["success"] is not None else 1,
    }


__all__ = ["RotationResult", "rotate_audit_log"]
