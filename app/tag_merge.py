"""Merge one tag into another, moving every assignment and deleting the source.

This module is the "fix a typo / consolidate duplicates" admin tool. It
operates by *name*, not by id, because operators reach for it from the
UI when they spot two near-identical tags (``passwrod``/``password``,
``meet``/``meeting``) and want to fold one into the other without
losing the screenshots already labelled with the wrong one.

Semantics
---------
* **Source** is the tag being eliminated; **destination** is the
  surviving tag. After a successful merge the destination owns every
  screenshot that previously had either tag, and the source row is
  gone.
* When a screenshot already carries *both* the source and the
  destination tag, the primary key ``(screenshot_id, tag_id)`` would
  collide on a naive ``UPDATE``; we use ``INSERT OR IGNORE ... SELECT``
  + ``DELETE`` so the dedup happens at SQL level and we never raise on
  a perfectly normal overlap.
* The entire mutation runs inside a single transaction (``BEGIN`` …
  ``COMMIT`` / ``ROLLBACK``). Any aiosqlite error rolls back so we
  never leave a half-merged state on disk.
* ``dry_run=True`` (default) inspects the world but never writes, so
  the admin UI can render an honest "this will move N assignments"
  preview before asking for confirmation.

Notes for callers
-----------------
* Name comparison is case-insensitive and whitespace-trimmed — mirrors
  how :func:`app.storage.tags.create_tag` normalises names.
* Merging a tag into itself is a no-op (``moved=0``).
* The audit row (``action="tag.merge"``) is written *after* the
  transaction commits, so a failed merge never leaves a misleading
  success entry in the audit log.
"""

from __future__ import annotations

from typing import TypedDict

import aiosqlite

from app.audit import log_action
from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.tag_merge")


class TagMergeResult(TypedDict):
    """Outcome summary returned by :func:`merge_tags`.

    Attributes:
        moved: Number of screenshot ↔ tag links that were re-pointed at
            the destination tag. For a dry-run this is the number of
            links that *would* be moved.
        source_existed: ``True`` when a tag matching ``source_name``
            was found at the time of the call.
        dest_existed: ``True`` when a tag matching ``dest_name`` was
            found at the time of the call.
        dry_run: Echoes the ``dry_run`` argument so the caller can
            tell preview output from real output at a glance.
    """

    moved: int
    source_existed: bool
    dest_existed: bool
    dry_run: bool


def _normalise(name: str) -> str:
    """Lowercase + strip — same rule used by :func:`app.storage.tags.create_tag`."""
    return (name or "").strip().lower()


async def _lookup_tag_id(conn: aiosqlite.Connection, name: str) -> int | None:
    """Return the id of the tag named ``name``, or ``None`` if no such tag exists."""
    cursor = await conn.execute("SELECT id FROM tags WHERE name = ?", (name,))
    row = await cursor.fetchone()
    if row is None:
        return None
    return int(row["id"])


async def _count_assignments(conn: aiosqlite.Connection, tag_id: int) -> int:
    """Return the number of screenshots currently linked to ``tag_id``."""
    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM screenshot_tags WHERE tag_id = ?",
        (tag_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return 0
    return int(row["n"])


async def merge_tags(
    source_name: str,
    dest_name: str,
    dry_run: bool = True,
) -> TagMergeResult:
    """Merge ``source_name`` into ``dest_name``.

    Steps inside a single transaction:

    1. Verify both tags exist; bail out with a populated
       :class:`TagMergeResult` (and ``moved=0``) when either is
       missing — callers display this as "Tag not found" in the UI.
    2. Move every ``screenshot_tags`` row that points at the source so
       it points at the destination instead, using
       ``INSERT OR IGNORE ... SELECT`` to harmlessly skip rows where
       the screenshot already carried the destination tag.
    3. Delete any leftover ``screenshot_tags`` rows pointing at the
       source (the ones that were skipped in step 2 because the
       destination already had them).
    4. Delete the now-orphan ``tags`` row for the source.
    5. Emit a structlog event and an audit row (``action="tag.merge"``)
       for the privileged action.

    Parameters
    ----------
    source_name:
        Tag to eliminate. Lookup is case-insensitive and
        whitespace-trimmed.
    dest_name:
        Tag that should survive and inherit every assignment from the
        source.
    dry_run:
        When ``True`` (default) nothing is written. The returned
        ``moved`` count reflects how many assignments *would* land on
        the destination if the same call were re-issued with
        ``dry_run=False``.

    Returns
    -------
    TagMergeResult
        Summary suitable for both the HTMX preview fragment and the
        confirmation fragment.
    """
    source = _normalise(source_name)
    dest = _normalise(dest_name)

    if not source or not dest:
        log.warning("tag_merge.empty_name", source=source, dest=dest)
        return TagMergeResult(
            moved=0,
            source_existed=False,
            dest_existed=False,
            dry_run=dry_run,
        )

    if source == dest:
        log.info("tag_merge.noop_same_name", name=source)
        # Re-use a lookup so the caller still gets accurate
        # ``*_existed`` flags for the UI.
        async with get_connection() as conn:
            existing = await _lookup_tag_id(conn, source)
        exists = existing is not None
        return TagMergeResult(
            moved=0,
            source_existed=exists,
            dest_existed=exists,
            dry_run=dry_run,
        )

    async with get_connection() as conn:
        source_id = await _lookup_tag_id(conn, source)
        dest_id = await _lookup_tag_id(conn, dest)

        if source_id is None or dest_id is None:
            log.info(
                "tag_merge.missing",
                source=source,
                dest=dest,
                source_existed=source_id is not None,
                dest_existed=dest_id is not None,
            )
            return TagMergeResult(
                moved=0,
                source_existed=source_id is not None,
                dest_existed=dest_id is not None,
                dry_run=dry_run,
            )

        if dry_run:
            # Count rows on the source side that would *land* on the
            # destination — including duplicates that ``INSERT OR
            # IGNORE`` would drop. We report the full source count so
            # operators see the blast radius they're confirming.
            moved_preview = await _count_assignments(conn, source_id)
            log.info(
                "tag_merge.dry_run",
                source=source,
                dest=dest,
                moved=moved_preview,
            )
            return TagMergeResult(
                moved=moved_preview,
                source_existed=True,
                dest_existed=True,
                dry_run=True,
            )

        moved = 0
        try:
            await conn.execute("BEGIN")
            # Step 1 — copy assignments to the destination, skipping
            # any (screenshot, dest_tag) pairs that already exist.
            await conn.execute(
                "INSERT OR IGNORE INTO screenshot_tags (screenshot_id, tag_id, created_at) "
                "SELECT screenshot_id, ?, created_at FROM screenshot_tags WHERE tag_id = ?",
                (dest_id, source_id),
            )
            # Source-side row count *is* the "moved" total for the UI:
            # every one of those rows either lands on the destination
            # (new pair) or was already there (existing pair). Either
            # way the destination owns the screenshot after the merge.
            moved = await _count_assignments(conn, source_id)
            # Step 2 — drop the source-side links so the source tag is
            # safe to delete (the screenshot_tags row would otherwise
            # block via the PK / FK).
            await conn.execute(
                "DELETE FROM screenshot_tags WHERE tag_id = ?",
                (source_id,),
            )
            # Step 3 — finally delete the tag row itself.
            await conn.execute("DELETE FROM tags WHERE id = ?", (source_id,))
            await conn.commit()
        except aiosqlite.Error:
            await conn.rollback()
            log.exception(
                "tag_merge.failed",
                source=source,
                dest=dest,
            )
            raise

    log.info(
        "tag_merge.applied",
        source=source,
        dest=dest,
        moved=moved,
    )
    await log_action(
        "tag.merge",
        target=f"{source}->{dest}",
        detail=f"{moved} assignments moved",
    )
    return TagMergeResult(
        moved=moved,
        source_existed=True,
        dest_existed=True,
        dry_run=False,
    )


__all__ = ["TagMergeResult", "merge_tags"]
