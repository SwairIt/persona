"""Bulk-delete screenshots that match an FTS5 search query.

This is the destructive sibling of :mod:`app.bulk_tag`. It defaults to
``dry_run=True`` so callers must explicitly opt-in to actually wiping rows.

Reuses :func:`app.search.search` for the MATCH so the FTS5 SQL lives in
exactly one place. The actual ``DELETE FROM screenshots WHERE id IN (...)``
relies on the FK cascades + FTS5 triggers (defined in the migrations) to
clean up related rows (tags, notes, embeddings, the FTS index, etc.).

Thumbnail files on disk are removed via :mod:`anyio.to_thread` so we never
block the event loop on filesystem I/O.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import anyio.to_thread

from app.logging_setup import get_logger
from app.search import search as fts_search
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.bulk.delete")


class BulkDeleteResult(TypedDict):
    """Outcome summary returned by :func:`bulk_delete`."""

    matched: int
    deleted: int
    dry_run: bool
    ids: list[int]


async def _resolve_matching(
    conn: aiosqlite.Connection,
    *,
    query: str,
    limit: int,
) -> tuple[list[int], list[str]]:
    """Return matched screenshot ids + their thumbnail paths via FTS5 search."""
    hits = await fts_search(conn, query=query, limit=limit)
    ids = [hit.screenshot_id for hit in hits]
    if not ids:
        return [], []

    # Fetch thumbnail paths in a single round-trip (search() doesn't expose
    # whether the path is missing vs empty string consistently across rows).
    placeholders = ",".join("?" for _ in ids)
    cursor = await conn.execute(
        f"SELECT thumbnail_path FROM screenshots WHERE id IN ({placeholders})",  # noqa: S608 — placeholders are only "?"
        ids,
    )
    rows = await cursor.fetchall()
    thumbs = [str(row["thumbnail_path"]) for row in rows if row["thumbnail_path"]]
    return ids, thumbs


def _unlink_files(paths: list[str]) -> int:
    """Delete files on disk, return how many were actually removed.

    Synchronous worker — run via :func:`anyio.to_thread.run_sync`. Missing
    files and OSError are swallowed so a permission glitch on one thumbnail
    cannot abort the whole batch.
    """
    removed = 0
    for raw in paths:
        path = Path(raw)
        try:
            if path.exists():
                path.unlink()
                removed += 1
        except OSError as exc:  # pragma: no cover - best-effort cleanup
            log.warning("bulk.delete.thumb_failed", path=str(path), error=str(exc))
    return removed


async def bulk_delete(
    query: str,
    limit: int,
    dry_run: bool = True,
) -> BulkDeleteResult:
    """Delete every screenshot whose FTS5 MATCH on ``query`` succeeds.

    Defaults to ``dry_run=True`` — caller MUST pass ``dry_run=False`` to
    actually touch the DB. Even when deleting, ``limit`` caps the blast
    radius so a typo cannot wipe the whole memory.

    Returns a :class:`BulkDeleteResult` summarising what happened.
    """
    async with get_connection() as conn:
        ids, thumbs = await _resolve_matching(conn, query=query, limit=limit)

        if dry_run:
            log.info(
                "bulk.delete",
                query=query,
                matched=len(ids),
                deleted=0,
                dry_run=True,
            )
            return BulkDeleteResult(
                matched=len(ids),
                deleted=0,
                dry_run=True,
                ids=ids,
            )

        if not ids:
            log.info(
                "bulk.delete",
                query=query,
                matched=0,
                deleted=0,
                dry_run=False,
            )
            return BulkDeleteResult(
                matched=0,
                deleted=0,
                dry_run=False,
                ids=[],
            )

        placeholders = ",".join("?" for _ in ids)
        cursor = await conn.execute(
            f"DELETE FROM screenshots WHERE id IN ({placeholders})",  # noqa: S608 — placeholders are only "?"
            ids,
        )
        deleted = int(cursor.rowcount or 0)
        await conn.commit()

    if thumbs:
        await anyio.to_thread.run_sync(_unlink_files, thumbs)

    log.info(
        "bulk.delete",
        query=query,
        matched=len(ids),
        deleted=deleted,
        dry_run=False,
    )
    return BulkDeleteResult(
        matched=len(ids),
        deleted=deleted,
        dry_run=False,
        ids=ids,
    )


__all__ = ["BulkDeleteResult", "bulk_delete"]
