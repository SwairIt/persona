"""Bulk-favourite / bulk-unfavourite screenshots driven by an FTS5 search.

The discovery-shortcut sibling of :mod:`app.bulk_pin`: where pin flips the
storage tier so retention leaves a shot alone, this module flips the
``favourite`` star — the quick-access bookmark a user sees on
``/favourites``. The two are deliberately independent (migration 027): a
shot may be pinned, favourited, both, or neither.

Defaults to ``dry_run=True`` so callers must explicitly opt-in to actually
mutating ``favourite`` rows — same blast-radius posture as
:func:`app.bulk_pin.bulk_pin`.

Reuses :func:`app.search.search` for the MATCH so the FTS5 SQL lives in
exactly one place. The per-row helpers ``INSERT OR IGNORE`` /
``DELETE`` are kept inline rather than splitting into a dedicated storage
module: there is exactly one writer in the codebase
(:mod:`app.web.routes.favourites`) and it ships the same one-liner. We
share the *contract* (idempotent insert, hard delete) rather than the
function.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from app.logging_setup import get_logger
from app.search import search as fts_search
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.bulk_favourite")


class BulkFavouriteResult(TypedDict):
    """Outcome summary returned by :func:`bulk_favourite` and :func:`bulk_unfavourite`.

    ``favourited`` counts rows that were actually written (``INSERT OR
    IGNORE`` returns ``rowcount=1`` only when the row did not already
    exist). On a dry-run this is always ``0``. For :func:`bulk_unfavourite`
    the field carries the number of rows successfully removed.
    """

    matched: int
    favourited: int
    dry_run: bool
    ids: list[int]


async def _resolve_matching(
    conn: aiosqlite.Connection,
    *,
    query: str,
    limit: int,
) -> list[int]:
    """Run the shared FTS5 search and return matched screenshot ids only."""
    hits = await fts_search(conn, query=query, limit=limit)
    return [hit.screenshot_id for hit in hits]


async def bulk_favourite(
    query: str,
    limit: int,
    dry_run: bool = True,
) -> BulkFavouriteResult:
    """Star every screenshot whose FTS5 MATCH on ``query`` succeeds.

    Defaults to ``dry_run=True`` — callers MUST pass ``dry_run=False`` to
    actually write ``favourite`` rows. Even when starring for real,
    ``limit`` caps the blast radius so a typo cannot star the entire
    memory.

    Each matched row is added via ``INSERT OR IGNORE`` so calling twice
    is safe — a shot already in the table is left alone and counted in
    ``matched`` but not in ``favourited``. This matches the idempotent
    contract of :func:`app.web.routes.favourites.toggle_favourite`'s
    insert branch (same table, same SQL shape).

    Returns a :class:`BulkFavouriteResult` summarising what happened.
    """
    async with get_connection() as conn:
        ids = await _resolve_matching(conn, query=query, limit=limit)

        if dry_run:
            log.info(
                "bulk_favourite.dry_run",
                query=query,
                matched=len(ids),
                favourited=0,
            )
            return BulkFavouriteResult(
                matched=len(ids),
                favourited=0,
                dry_run=True,
                ids=ids,
            )

        if not ids:
            log.info(
                "bulk_favourite.empty",
                query=query,
                matched=0,
                favourited=0,
            )
            return BulkFavouriteResult(
                matched=0,
                favourited=0,
                dry_run=False,
                ids=[],
            )

        favourited = 0
        for screenshot_id in ids:
            cursor = await conn.execute(
                "INSERT OR IGNORE INTO favourite (screenshot_id) VALUES (?)",
                (screenshot_id,),
            )
            # ``rowcount`` is 1 when a new row was inserted, 0 when the
            # OR IGNORE clause swallowed a duplicate. Counting only real
            # writes keeps the result honest for the audit trail.
            if cursor.rowcount == 1:
                favourited += 1
        await conn.commit()

    log.info(
        "bulk_favourite.applied",
        query=query,
        matched=len(ids),
        favourited=favourited,
    )
    return BulkFavouriteResult(
        matched=len(ids),
        favourited=favourited,
        dry_run=False,
        ids=ids,
    )


async def bulk_unfavourite(
    query: str,
    limit: int,
) -> BulkFavouriteResult:
    """Un-star every screenshot whose FTS5 MATCH on ``query`` succeeds.

    Drops the ``favourite`` row for each matched shot. There is no
    ``dry_run`` parameter — un-favouriting is non-destructive (it only
    removes a discovery shortcut, the screenshot itself is untouched) —
    so the CLI/web layers always call this for real, mirroring
    :func:`app.bulk_pin.bulk_unpin`.

    The ``favourited`` field of the returned :class:`BulkFavouriteResult`
    carries the number of ``favourite`` rows actually removed (matched
    shots that were not starred contribute ``0``).
    """
    async with get_connection() as conn:
        ids = await _resolve_matching(conn, query=query, limit=limit)

        if not ids:
            log.info(
                "bulk_unfavourite.empty",
                query=query,
                matched=0,
                favourited=0,
            )
            return BulkFavouriteResult(
                matched=0,
                favourited=0,
                dry_run=False,
                ids=[],
            )

        unfavourited = 0
        for screenshot_id in ids:
            cursor = await conn.execute(
                "DELETE FROM favourite WHERE screenshot_id = ?",
                (screenshot_id,),
            )
            # ``rowcount`` is 1 when a starred row was removed, 0 when
            # the shot was not in the favourites table to begin with.
            if cursor.rowcount == 1:
                unfavourited += 1
        await conn.commit()

    log.info(
        "bulk_unfavourite.applied",
        query=query,
        matched=len(ids),
        favourited=unfavourited,
    )
    return BulkFavouriteResult(
        matched=len(ids),
        favourited=unfavourited,
        dry_run=False,
        ids=ids,
    )


__all__ = ["BulkFavouriteResult", "bulk_favourite", "bulk_unfavourite"]
