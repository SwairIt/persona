"""Soft-delete recycle bin for screenshots and standalone notes.

Items that the user deletes via :mod:`app.bulk_delete` or the screenshot
detail "delete" button do NOT vanish — they are first serialised into
``recycle_bin`` (see ``migrations/041_recycle_bin.sql``) and only
hard-deleted once :func:`purge_expired` decides they have outlived
``settings.recycle_retention_days``.

The two writes (insert into ``recycle_bin`` + delete from the source
table) run inside a single SQLite transaction so a crash mid-way cannot
leave the row in two places — or in neither.

Thumbnails on disk are KEPT until purge time: :func:`restore` needs them
to put the screenshot back exactly as it was. :func:`purge_expired`
unlinks the file as the last step, after the row is gone from
``recycle_bin``.

Filesystem I/O is routed through :func:`anyio.to_thread.run_sync` so we
never block the event loop on disk.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict

import anyio.to_thread

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage_savings import record_retention_freed

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.recycle")


class RecycleBinEntry(TypedDict):
    """Row returned by :func:`list_bin`."""

    id: int
    kind: str
    original_id: int
    payload: dict[str, Any]
    deleted_at: str
    thumbnail_path: str | None


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    """Convert an ``aiosqlite.Row`` to a plain ``dict`` for JSON payloads."""
    return {key: row[key] for key in row.keys()}  # noqa: SIM118 — aiosqlite.Row has no __contains__


async def soft_delete_screenshot(screenshot_id: int) -> int | None:
    """Move one screenshot into the recycle bin.

    Serialises the row to JSON, copies ``thumbnail_path`` into its own
    column, inserts into ``recycle_bin`` and then ``DELETE FROM
    screenshots`` — all inside a single transaction so a crash cannot
    duplicate or lose the row.

    Returns the new ``recycle_bin.id`` (or ``None`` when the screenshot
    does not exist).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM screenshots WHERE id = ?",
            (screenshot_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            log.warning("recycle.screenshot.missing", screenshot_id=screenshot_id)
            return None

        payload = _row_to_dict(row)
        thumbnail = payload.get("thumbnail_path")
        thumbnail_str: str | None = str(thumbnail) if thumbnail else None

        try:
            await conn.execute("BEGIN")
            insert = await conn.execute(
                """
                INSERT INTO recycle_bin (kind, original_id, payload, thumbnail_path)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "screenshot",
                    screenshot_id,
                    json.dumps(payload, default=str),
                    thumbnail_str,
                ),
            )
            await conn.execute(
                "DELETE FROM screenshots WHERE id = ?",
                (screenshot_id,),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            log.exception("recycle.screenshot.failed", screenshot_id=screenshot_id)
            raise

        recycle_id = insert.lastrowid
        log.info(
            "recycle.screenshot.soft_deleted",
            screenshot_id=screenshot_id,
            recycle_id=recycle_id,
        )
        return int(recycle_id) if recycle_id is not None else None


async def soft_delete_note(note_id: int) -> int | None:
    """Move one standalone note into the recycle bin.

    Same atomic insert+delete contract as :func:`soft_delete_screenshot`,
    just against the ``notes`` table. Notes have no thumbnail; the
    column stays ``NULL``.

    Returns the new ``recycle_bin.id`` (or ``None`` when the note does
    not exist).
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM notes WHERE id = ?",
            (note_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            log.warning("recycle.note.missing", note_id=note_id)
            return None

        payload = _row_to_dict(row)

        try:
            await conn.execute("BEGIN")
            insert = await conn.execute(
                """
                INSERT INTO recycle_bin (kind, original_id, payload, thumbnail_path)
                VALUES (?, ?, ?, ?)
                """,
                (
                    "note",
                    note_id,
                    json.dumps(payload, default=str),
                    None,
                ),
            )
            await conn.execute(
                "DELETE FROM notes WHERE id = ?",
                (note_id,),
            )
            await conn.commit()
        except Exception:
            await conn.rollback()
            log.exception("recycle.note.failed", note_id=note_id)
            raise

        recycle_id = insert.lastrowid
        log.info(
            "recycle.note.soft_deleted",
            note_id=note_id,
            recycle_id=recycle_id,
        )
        return int(recycle_id) if recycle_id is not None else None


async def restore(recycle_id: int) -> bool:
    """Re-insert one bin row back into its original table.

    Parses the stored JSON, ``INSERT``s back into either ``screenshots``
    or ``notes`` preserving the original ``id`` and column values, then
    ``DELETE``s the recycle row — all inside one transaction.

    Returns ``True`` on success, ``False`` when ``recycle_id`` is unknown.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, kind, original_id, payload, thumbnail_path FROM recycle_bin WHERE id = ?",
            (recycle_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            log.warning("recycle.restore.missing", recycle_id=recycle_id)
            return False

        kind = str(row["kind"])
        payload: dict[str, Any] = json.loads(str(row["payload"]))

        try:
            await conn.execute("BEGIN")
            if kind == "screenshot":
                await _reinsert_screenshot(conn, payload)
            elif kind == "note":
                await _reinsert_note(conn, payload)
            else:  # pragma: no cover — CHECK constraint forbids other values
                msg = f"unknown recycle kind: {kind}"
                raise RuntimeError(msg)
            await conn.execute("DELETE FROM recycle_bin WHERE id = ?", (recycle_id,))
            await conn.commit()
        except Exception:
            await conn.rollback()
            log.exception("recycle.restore.failed", recycle_id=recycle_id, kind=kind)
            raise

    log.info(
        "recycle.restored",
        recycle_id=recycle_id,
        kind=kind,
        original_id=int(row["original_id"]),
    )
    return True


async def _reinsert_screenshot(
    conn: aiosqlite.Connection,
    payload: dict[str, Any],
) -> None:
    """Push the serialised screenshot row back into ``screenshots``.

    Builds the INSERT dynamically so future column additions on
    ``screenshots`` survive without forcing this function to change —
    as long as the payload was serialised from the same schema version.
    """
    columns = list(payload.keys())
    placeholders = ",".join("?" for _ in columns)
    column_list = ",".join(columns)
    values = [payload[col] for col in columns]
    await conn.execute(
        f"INSERT INTO screenshots ({column_list}) VALUES ({placeholders})",  # noqa: S608 — column names come from our own serialiser
        values,
    )


async def _reinsert_note(
    conn: aiosqlite.Connection,
    payload: dict[str, Any],
) -> None:
    """Push the serialised note row back into ``notes``."""
    columns = list(payload.keys())
    placeholders = ",".join("?" for _ in columns)
    column_list = ",".join(columns)
    values = [payload[col] for col in columns]
    await conn.execute(
        f"INSERT INTO notes ({column_list}) VALUES ({placeholders})",  # noqa: S608 — column names come from our own serialiser
        values,
    )


async def list_bin(limit: int = 100) -> list[RecycleBinEntry]:
    """Return the most-recently soft-deleted rows, newest first."""
    capped = max(1, min(int(limit), 1000))
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, kind, original_id, payload, deleted_at, thumbnail_path
            FROM recycle_bin
            ORDER BY deleted_at DESC, id DESC
            LIMIT ?
            """,
            (capped,),
        )
        rows = await cursor.fetchall()

    entries: list[RecycleBinEntry] = []
    for row in rows:
        try:
            parsed: dict[str, Any] = json.loads(str(row["payload"]))
        except json.JSONDecodeError:
            parsed = {}
        entries.append(
            RecycleBinEntry(
                id=int(row["id"]),
                kind=str(row["kind"]),
                original_id=int(row["original_id"]),
                payload=parsed,
                deleted_at=str(row["deleted_at"]),
                thumbnail_path=(
                    str(row["thumbnail_path"]) if row["thumbnail_path"] else None
                ),
            )
        )
    return entries


def _unlink_files(paths: list[str]) -> tuple[int, int]:
    """Delete files on disk; return ``(removed_count, bytes_freed)``.

    Synchronous worker — invoked via :func:`anyio.to_thread.run_sync`.
    Missing files and :class:`OSError` are swallowed so a single
    permission glitch cannot abort the whole purge. The byte tally is
    captured via :py:meth:`pathlib.Path.stat` *before* :py:meth:`unlink`
    because the file is gone after the unlink — and we need the size
    to credit :func:`app.storage_savings.record_retention_freed`.
    """
    removed = 0
    bytes_freed = 0
    for raw in paths:
        path = Path(raw)
        try:
            if path.exists():
                size = path.stat().st_size
                path.unlink()
                removed += 1
                bytes_freed += int(size)
        except OSError as exc:  # pragma: no cover — best-effort cleanup
            log.warning("recycle.purge.thumb_failed", path=str(path), error=str(exc))
    return removed, bytes_freed


async def purge_expired(retention_days: int = 7) -> int:
    """Hard-delete every bin row older than ``retention_days``.

    Returns the on-disk **bytes reclaimed** by unlinking the thumbnail
    files of the purged rows. Thumbnail files for purged rows are
    unlinked in a thread pool so we never block the event loop on disk.

    The bytes total is also credited to today's row in
    ``storage_saving`` via :func:`app.storage_savings.record_retention_freed`
    so the savings chart can attribute the reclaim to the retention pass.
    """
    if retention_days < 1:
        msg = f"retention_days must be >= 1, got {retention_days}"
        raise ValueError(msg)

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    cutoff_iso = cutoff.strftime("%Y-%m-%d %H:%M:%S")

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, thumbnail_path FROM recycle_bin WHERE deleted_at < ?",
            (cutoff_iso,),
        )
        rows = await cursor.fetchall()
        if not rows:
            return 0

        ids = [int(row["id"]) for row in rows]
        thumbs = [
            str(row["thumbnail_path"]) for row in rows if row["thumbnail_path"]
        ]

        placeholders = ",".join("?" for _ in ids)
        await conn.execute(
            f"DELETE FROM recycle_bin WHERE id IN ({placeholders})",  # noqa: S608 — placeholders are only "?"
            ids,
        )
        await conn.commit()

    bytes_freed = 0
    thumbs_unlinked = 0
    if thumbs:
        thumbs_unlinked, bytes_freed = await anyio.to_thread.run_sync(
            _unlink_files, thumbs
        )

    # Credit the savings journal *after* the DELETE commits and the disk
    # work finishes so a failed unlink batch cannot leave a phantom bump
    # in ``storage_saving``. ``record_retention_freed`` no-ops on zero.
    await record_retention_freed(bytes_freed)

    log.info(
        "recycle.purge",
        purged=len(ids),
        thumbs_unlinked=thumbs_unlinked,
        bytes_freed=bytes_freed,
        retention_days=retention_days,
    )
    return bytes_freed


__all__ = [
    "RecycleBinEntry",
    "list_bin",
    "purge_expired",
    "restore",
    "soft_delete_note",
    "soft_delete_screenshot",
]
