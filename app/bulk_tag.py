"""Bulk tag/untag operations driven by an FTS5 search query.

Used by the CLI subcommands ``tag`` and ``untag`` to apply (or remove) a tag
across every screenshot whose OCR/title/app matches a free-text query.

Reuses :func:`app.search.search` for the FTS5 MATCH so we never re-implement
the FTS5 SQL.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from app.logging_setup import get_logger
from app.search import search as fts_search
from app.storage.db import get_connection
from app.storage.tags import create_tag, tag_screenshot, untag_screenshot

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.cli.tag")


class BulkTagResult(TypedDict):
    """Outcome summary returned by :func:`bulk_tag` and :func:`bulk_untag`."""

    tag: str
    query: str
    matched: int
    affected: int
    dry_run: bool


async def _resolve_matching_ids(
    conn: aiosqlite.Connection,
    *,
    query: str,
    limit: int,
) -> list[int]:
    """Run the existing FTS5 search and return matched screenshot ids only."""
    hits = await fts_search(conn, query=query, limit=limit)
    return [hit.screenshot_id for hit in hits]


async def bulk_tag(
    tag: str,
    query: str,
    limit: int,
    dry_run: bool,
) -> BulkTagResult:
    """Apply ``tag`` to every screenshot whose FTS5 MATCH on ``query`` succeeds.

    The tag row is created on demand (idempotent). The screenshot ↔ tag link
    is inserted via ``INSERT OR IGNORE`` so calling twice is safe.

    Returns a :class:`BulkTagResult` describing what happened.
    """
    normalised = tag.strip().lower()
    async with get_connection() as conn:
        screenshot_ids = await _resolve_matching_ids(conn, query=query, limit=limit)

        if dry_run:
            log.info(
                "bulk_tag.dry_run",
                tag=normalised,
                query=query,
                matched=len(screenshot_ids),
            )
            return BulkTagResult(
                tag=normalised,
                query=query,
                matched=len(screenshot_ids),
                affected=len(screenshot_ids),
                dry_run=True,
            )

        if not screenshot_ids:
            log.info("bulk_tag.empty", tag=normalised, query=query)
            return BulkTagResult(
                tag=normalised,
                query=query,
                matched=0,
                affected=0,
                dry_run=False,
            )

        tag_id = await create_tag(conn, name=normalised)
        for screenshot_id in screenshot_ids:
            await tag_screenshot(conn, screenshot_id, tag_id)

    log.info(
        "bulk_tag.applied",
        tag=normalised,
        query=query,
        matched=len(screenshot_ids),
    )
    return BulkTagResult(
        tag=normalised,
        query=query,
        matched=len(screenshot_ids),
        affected=len(screenshot_ids),
        dry_run=False,
    )


async def bulk_untag(
    tag: str,
    query: str,
    limit: int,
) -> BulkTagResult:
    """Remove ``tag`` from every screenshot whose FTS5 MATCH on ``query`` succeeds.

    If the tag row does not exist we short-circuit with ``affected=0``.
    """
    normalised = tag.strip().lower()
    async with get_connection() as conn:
        cursor = await conn.execute("SELECT id FROM tags WHERE name = ?", (normalised,))
        row = await cursor.fetchone()
        if row is None:
            log.info("bulk_untag.no_tag", tag=normalised, query=query)
            return BulkTagResult(
                tag=normalised,
                query=query,
                matched=0,
                affected=0,
                dry_run=False,
            )
        tag_id = int(row["id"])

        screenshot_ids = await _resolve_matching_ids(conn, query=query, limit=limit)
        for screenshot_id in screenshot_ids:
            await untag_screenshot(conn, screenshot_id, tag_id)

    log.info(
        "bulk_untag.applied",
        tag=normalised,
        query=query,
        matched=len(screenshot_ids),
    )
    return BulkTagResult(
        tag=normalised,
        query=query,
        matched=len(screenshot_ids),
        affected=len(screenshot_ids),
        dry_run=False,
    )


__all__ = ["BulkTagResult", "bulk_tag", "bulk_untag"]
