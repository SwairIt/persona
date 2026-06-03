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
from app.recycle import ShotLocked, soft_delete_screenshot
from app.search import search as fts_search
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.bulk.delete")


class BulkDeleteResult(TypedDict):
    """Outcome summary returned by :func:`bulk_delete`.

    ``skipped_locked`` (v0.70) counts FTS hits that were filtered out
    because their row carries ``screenshots.locked = 1``. It is always
    populated, even on dry-runs, so the operator can see *before*
    confirming the destructive call how many of the matched shots will
    actually move.
    """

    matched: int
    deleted: int
    dry_run: bool
    ids: list[int]
    skipped_locked: int


async def _resolve_matching(
    conn: aiosqlite.Connection,
    *,
    query: str,
    limit: int,
) -> list[int]:
    """Return matched screenshot ids via FTS5 search."""
    hits = await fts_search(conn, query=query, limit=limit)
    return [hit.screenshot_id for hit in hits]


async def _filter_locked(
    conn: aiosqlite.Connection,
    ids: list[int],
) -> tuple[list[int], int]:
    """Drop ids whose row has ``locked = 1``. Return ``(kept, skipped_count)``.

    v0.70 — per-shot lock guard. A locked screenshot is excluded from
    every bulk delete path so a typo'd FTS query cannot evict a user's
    most-treasured frames. The filter is done in SQL with a single
    ``WHERE locked = 1`` probe over the candidate id set, which keeps
    the round-trip count constant regardless of how many ids the FTS
    search returned. Empty input short-circuits — no DB hit.
    """
    if not ids:
        return [], 0
    placeholders = ",".join("?" for _ in ids)
    cursor = await conn.execute(
        f"SELECT id FROM screenshots WHERE locked = 1 AND id IN ({placeholders})",  # noqa: S608 — placeholders are only "?"
        ids,
    )
    rows = await cursor.fetchall()
    locked_set = {int(row["id"]) for row in rows}
    kept = [shot_id for shot_id in ids if shot_id not in locked_set]
    return kept, len(locked_set)


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
        matched_ids = await _resolve_matching(conn, query=query, limit=limit)
        # v0.70 — locked shots are filtered out *before* the dry-run
        # report so the operator sees the real blast radius up front.
        deletable_ids, skipped_locked = await _filter_locked(conn, matched_ids)

    if dry_run:
        log.info(
            "bulk.delete",
            query=query,
            matched=len(matched_ids),
            deleted=0,
            skipped_locked=skipped_locked,
            dry_run=True,
        )
        return BulkDeleteResult(
            matched=len(matched_ids),
            deleted=0,
            dry_run=True,
            ids=deletable_ids,
            skipped_locked=skipped_locked,
        )

    if not deletable_ids:
        log.info(
            "bulk.delete",
            query=query,
            matched=len(matched_ids),
            deleted=0,
            skipped_locked=skipped_locked,
            dry_run=False,
        )
        return BulkDeleteResult(
            matched=len(matched_ids),
            deleted=0,
            dry_run=False,
            ids=[],
            skipped_locked=skipped_locked,
        )

    deleted = 0
    for screenshot_id in deletable_ids:
        # TOCTOU: a concurrent tab could have locked this id between
        # ``_filter_locked`` and now. Treat the late-lock as a skip
        # rather than a 500 — the user clearly meant to keep the row.
        try:
            recycle_id = await soft_delete_screenshot(screenshot_id)
        except ShotLocked:
            skipped_locked += 1
            log.info(
                "bulk.delete.skipped_locked_race",
                query=query,
                screenshot_id=screenshot_id,
            )
            continue
        if recycle_id is not None:
            deleted += 1

    log.info(
        "bulk.delete",
        query=query,
        matched=len(matched_ids),
        deleted=deleted,
        skipped_locked=skipped_locked,
        dry_run=False,
    )
    return BulkDeleteResult(
        matched=len(matched_ids),
        deleted=deleted,
        dry_run=False,
        ids=deletable_ids,
        skipped_locked=skipped_locked,
    )


__all__ = ["BulkDeleteResult", "bulk_delete"]
