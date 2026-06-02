"""Bulk-delete screenshots that match an FTS5 search query.

This is the destructive sibling of :mod:`app.bulk_tag`. It defaults to
``dry_run=True`` so callers must explicitly opt-in to actually wiping rows.

Reuses :func:`app.search.search` for the MATCH so the FTS5 SQL lives in
exactly one place. As of v0.40 the actual deletion is routed per-id
through :func:`app.recycle.soft_delete_screenshot` so wiped rows land in
the recycle bin first; the retention worker hard-deletes them after
``settings.recycle_retention_days``.

Thumbnail files on disk stay put until the recycle bin purges them —
that's how :func:`app.recycle.restore` can put a screenshot back exactly
as it was.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from app.logging_setup import get_logger
from app.recycle import soft_delete_screenshot
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
) -> list[int]:
    """Return matched screenshot ids via FTS5 search."""
    hits = await fts_search(conn, query=query, limit=limit)
    return [hit.screenshot_id for hit in hits]


async def bulk_delete(
    query: str,
    limit: int,
    dry_run: bool = True,
) -> BulkDeleteResult:
    """Soft-delete every screenshot whose FTS5 MATCH on ``query`` succeeds.

    Defaults to ``dry_run=True`` — caller MUST pass ``dry_run=False`` to
    actually touch the DB. Even when deleting, ``limit`` caps the blast
    radius so a typo cannot wipe the whole memory.

    As of v0.40 each matched row is routed through
    :func:`app.recycle.soft_delete_screenshot`, which atomically copies
    the row into ``recycle_bin`` and then deletes from ``screenshots``.
    Thumbnails stay on disk until the retention worker purges the bin —
    that way :func:`app.recycle.restore` can resurrect the screenshot
    fully intact within the retention window.

    Returns a :class:`BulkDeleteResult` summarising what happened.
    """
    async with get_connection() as conn:
        ids = await _resolve_matching(conn, query=query, limit=limit)

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

    deleted = 0
    for screenshot_id in ids:
        recycle_id = await soft_delete_screenshot(screenshot_id)
        if recycle_id is not None:
            deleted += 1

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
