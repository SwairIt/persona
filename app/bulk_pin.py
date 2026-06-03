"""Bulk-pin / bulk-unpin screenshots driven by an FTS5 search query.

This is the protective sibling of :mod:`app.bulk_tag` and the inverse of
:mod:`app.bulk_delete`: instead of attaching a tag or wiping rows it flips
each matching screenshot's storage tier to ``pinned`` so the retention
worker (:mod:`app.workers.retention`) never demotes them to ``warm`` /
``cold`` and never sweeps them out from under the user.

Defaults to ``dry_run=True`` so callers must explicitly opt-in to actually
mutate tiers — the same blast-radius posture as :func:`app.bulk_delete`.

Reuses :func:`app.search.search` for the MATCH so the FTS5 SQL lives in
exactly one place, and the existing per-id helpers
:func:`app.storage.tiers.pin_screenshot` /
:func:`app.storage.tiers.unpin_screenshot` so a tier flip happens the same
way it does through the per-shot pin endpoint in :mod:`app.web.routes.pin`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from app.logging_setup import get_logger
from app.search import search as fts_search
from app.storage.db import get_connection
from app.storage.tiers import pin_screenshot, unpin_screenshot

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.bulk_pin")


class BulkPinResult(TypedDict):
    """Outcome summary returned by :func:`bulk_pin` and :func:`bulk_unpin`."""

    matched: int
    pinned: int
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


async def bulk_pin(
    query: str,
    limit: int,
    dry_run: bool = True,
) -> BulkPinResult:
    """Pin every screenshot whose FTS5 MATCH on ``query`` succeeds.

    Defaults to ``dry_run=True`` — callers MUST pass ``dry_run=False`` to
    actually mutate the tier column. Even when pinning for real, ``limit``
    caps the blast radius so a typo cannot pin the entire memory.

    Each matched row is routed through
    :func:`app.storage.tiers.pin_screenshot` so the SQL that writes
    ``tier = 'pinned'`` lives in exactly one place. Pinning is idempotent —
    re-pinning a pinned row is a no-op write.

    Returns a :class:`BulkPinResult` summarising what happened. The
    ``pinned`` count is ``0`` on a dry-run and equals ``matched`` on a
    successful real run (every helper call is a one-row UPDATE that
    succeeds for any row produced by the FTS5 search above).
    """
    async with get_connection() as conn:
        ids = await _resolve_matching(conn, query=query, limit=limit)

        if dry_run:
            log.info(
                "bulk_pin.dry_run",
                query=query,
                matched=len(ids),
                pinned=0,
            )
            return BulkPinResult(
                matched=len(ids),
                pinned=0,
                dry_run=True,
                ids=ids,
            )

        if not ids:
            log.info(
                "bulk_pin.empty",
                query=query,
                matched=0,
                pinned=0,
            )
            return BulkPinResult(
                matched=0,
                pinned=0,
                dry_run=False,
                ids=[],
            )

        pinned = 0
        for screenshot_id in ids:
            await pin_screenshot(conn, screenshot_id)
            pinned += 1

    log.info(
        "bulk_pin.applied",
        query=query,
        matched=len(ids),
        pinned=pinned,
    )
    return BulkPinResult(
        matched=len(ids),
        pinned=pinned,
        dry_run=False,
        ids=ids,
    )


async def bulk_unpin(
    query: str,
    limit: int,
) -> BulkPinResult:
    """Un-pin every screenshot whose FTS5 MATCH on ``query`` succeeds.

    Drops each matched row back to the ``hot`` tier via
    :func:`app.storage.tiers.unpin_screenshot`; the regular tier-sweep is
    then free to demote them to ``warm`` / ``cold`` as usual.

    There is no ``dry_run`` parameter — un-pinning is non-destructive
    (it never deletes data, it merely re-exposes shots to the existing
    retention policy) — so the CLI/web layers always call this for real.
    The ``pinned`` field of the returned :class:`BulkPinResult` carries
    the number of rows successfully un-pinned for symmetry with
    :func:`bulk_pin`.
    """
    async with get_connection() as conn:
        ids = await _resolve_matching(conn, query=query, limit=limit)

        if not ids:
            log.info(
                "bulk_unpin.empty",
                query=query,
                matched=0,
                pinned=0,
            )
            return BulkPinResult(
                matched=0,
                pinned=0,
                dry_run=False,
                ids=[],
            )

        unpinned = 0
        for screenshot_id in ids:
            await unpin_screenshot(conn, screenshot_id)
            unpinned += 1

    log.info(
        "bulk_unpin.applied",
        query=query,
        matched=len(ids),
        pinned=unpinned,
    )
    return BulkPinResult(
        matched=len(ids),
        pinned=unpinned,
        dry_run=False,
        ids=ids,
    )


__all__ = ["BulkPinResult", "bulk_pin", "bulk_unpin"]
