"""Bulk lock / unlock screenshots driven by an FTS5 search query (v0.83).

This is the bulk-shaped sibling of :mod:`app.web.routes.shot_lock`, which
exposes a per-shot ``POST /api/screenshot/{id}/lock`` toggle in the web
UI. The CLI flavour lets an operator flip ``screenshots.locked`` across
every screenshot whose FTS5 MATCH on ``query`` succeeds — useful for
shielding a whole project's worth of frames from accidental deletion in
one shot, or unlocking a stale archive before a planned cleanup.

Shape contract
--------------
Both helpers reuse :func:`app.search.search` for the FTS5 MATCH so the
search SQL lives in exactly one place (same posture as
:mod:`app.bulk_tag` / :mod:`app.bulk_pin` / :mod:`app.bulk_delete`). The
write itself is a one-row ``UPDATE screenshots SET locked = ?``; we
intentionally skip the per-id helper layer because there is no shared
``lock_screenshot`` function (the web route inlines the UPDATE for the
same reason — locking is a one-column flag, not a tier transition).

Lock is idempotent: locking an already-locked row and unlocking an
already-unlocked row are both no-op writes. We still report the count as
``affected`` because the caller asked for that state and we delivered it
— mirrors the ``INSERT OR IGNORE`` semantics in :func:`app.bulk_tag`.

Audit + structured logging
--------------------------
Every real (non dry-run) call emits an :func:`app.audit.log_action` row
under ``shot.bulk_lock`` / ``shot.bulk_unlock`` with the query as the
target and the affected count in ``detail``. The ``persona.shot_lock_cli``
structlog channel mirrors the same fields so an operator can tail logs
during a security review. Dry-runs do not audit — they are pure reads,
nothing actually changed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from app.audit import log_action
from app.logging_setup import get_logger
from app.search import search as fts_search
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.shot_lock_cli")


class BulkLockResult(TypedDict):
    """Outcome summary returned by :func:`lock_shots` and :func:`unlock_shots`.

    ``matched`` is the raw FTS5 hit count (capped by ``limit``).
    ``affected`` equals ``matched`` after a real run and is ``0`` on a
    dry-run; the call is otherwise symmetric so the caller can treat the
    two paths uniformly.
    """

    query: str
    matched: int
    affected: int
    dry_run: bool
    locked: bool
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


async def _set_locked(
    conn: aiosqlite.Connection,
    ids: list[int],
    *,
    locked: bool,
) -> None:
    """Flip ``screenshots.locked`` to ``locked`` for every id in ``ids``.

    Issued inside a single ``BEGIN`` / ``COMMIT`` so a partial failure
    rolls every row back together — locking half a query's worth of
    matches would be worse than locking none of them, because the user
    would then have to manually reconcile which shots are actually
    protected.
    """
    if not ids:
        return
    new_value = 1 if locked else 0
    try:
        await conn.execute("BEGIN")
        for screenshot_id in ids:
            await conn.execute(
                "UPDATE screenshots SET locked = ? WHERE id = ?",
                (new_value, screenshot_id),
            )
        await conn.commit()
    except Exception:
        await conn.rollback()
        raise


async def lock_shots(
    query: str,
    limit: int,
    dry_run: bool = True,
) -> BulkLockResult:
    """Lock every screenshot whose FTS5 MATCH on ``query`` succeeds.

    Defaults to ``dry_run=True`` — callers MUST pass ``dry_run=False`` to
    actually flip ``screenshots.locked`` to ``1``. ``limit`` caps the
    blast radius so a typo cannot lock the entire memory.

    Locking is idempotent: a row that is already locked stays locked and
    counts toward ``affected``. Each successful real run records one
    audit-log row under ``shot.bulk_lock`` so a security review can see
    which query protected which shots without ever touching the
    underlying payload.
    """
    async with get_connection() as conn:
        ids = await _resolve_matching(conn, query=query, limit=limit)

        if dry_run:
            log.info(
                "shot_lock_cli.lock.dry_run",
                query=query,
                matched=len(ids),
            )
            return BulkLockResult(
                query=query,
                matched=len(ids),
                affected=0,
                dry_run=True,
                locked=True,
                ids=ids,
            )

        if not ids:
            log.info(
                "shot_lock_cli.lock.empty",
                query=query,
                matched=0,
            )
            return BulkLockResult(
                query=query,
                matched=0,
                affected=0,
                dry_run=False,
                locked=True,
                ids=[],
            )

        try:
            await _set_locked(conn, ids, locked=True)
        except Exception:
            log.exception(
                "shot_lock_cli.lock.failed",
                query=query,
                matched=len(ids),
            )
            raise

    log.info(
        "shot_lock_cli.lock.applied",
        query=query,
        matched=len(ids),
        affected=len(ids),
    )
    await log_action(
        "shot.bulk_lock",
        target=query,
        detail=f"locked {len(ids)} screenshot(s)",
    )
    return BulkLockResult(
        query=query,
        matched=len(ids),
        affected=len(ids),
        dry_run=False,
        locked=True,
        ids=ids,
    )


async def unlock_shots(
    query: str,
    limit: int,
    dry_run: bool = True,
) -> BulkLockResult:
    """Unlock every screenshot whose FTS5 MATCH on ``query`` succeeds.

    Defaults to ``dry_run=True`` for symmetry with :func:`lock_shots` —
    unlocking is technically non-destructive (the row stays put, only
    its lock flag flips off) but it strips a guard the user explicitly
    asked for, so we keep the same opt-in confirmation shape as
    :func:`bulk_delete` / :func:`bulk_pin`.

    Unlocking is idempotent: a row that is already unlocked stays
    unlocked and counts toward ``affected``. Each successful real run
    records one audit-log row under ``shot.bulk_unlock``.
    """
    async with get_connection() as conn:
        ids = await _resolve_matching(conn, query=query, limit=limit)

        if dry_run:
            log.info(
                "shot_lock_cli.unlock.dry_run",
                query=query,
                matched=len(ids),
            )
            return BulkLockResult(
                query=query,
                matched=len(ids),
                affected=0,
                dry_run=True,
                locked=False,
                ids=ids,
            )

        if not ids:
            log.info(
                "shot_lock_cli.unlock.empty",
                query=query,
                matched=0,
            )
            return BulkLockResult(
                query=query,
                matched=0,
                affected=0,
                dry_run=False,
                locked=False,
                ids=[],
            )

        try:
            await _set_locked(conn, ids, locked=False)
        except Exception:
            log.exception(
                "shot_lock_cli.unlock.failed",
                query=query,
                matched=len(ids),
            )
            raise

    log.info(
        "shot_lock_cli.unlock.applied",
        query=query,
        matched=len(ids),
        affected=len(ids),
    )
    await log_action(
        "shot.bulk_unlock",
        target=query,
        detail=f"unlocked {len(ids)} screenshot(s)",
    )
    return BulkLockResult(
        query=query,
        matched=len(ids),
        affected=len(ids),
        dry_run=False,
        locked=False,
        ids=ids,
    )


__all__ = ["BulkLockResult", "lock_shots", "unlock_shots"]
