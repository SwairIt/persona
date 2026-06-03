"""Tag cleanup helpers — find orphan tags, purge them, untag stale shots.

This module is the housekeeping companion to :mod:`app.tag_merge`. Where
the merge tool consolidates near-duplicate tags, this one mops up after
bulk-delete and time-bounded labelling campaigns:

* :func:`find_orphan_tags` lists tags that have zero rows in
  ``screenshot_tags`` — typically left behind when every screenshot they
  pointed at was bulk-deleted.
* :func:`purge_orphan_tags` removes those rows in a single transaction
  and returns the number that were dropped.
* :func:`untag_older_than` removes a single tag from screenshots whose
  ``captured_at`` is older than ``days`` days — handy for "auto-tagged
  during the noisy week" rollback campaigns.

Design notes
------------
* All SQL uses ``?`` placeholders. No tag name or numeric argument is
  ever interpolated into a query string.
* Every mutation runs inside a single transaction (``BEGIN`` /
  ``COMMIT`` / ``ROLLBACK``) so a mid-flight failure never leaves a
  half-cleaned state on disk.
* Tag name comparison is case-insensitive and whitespace-trimmed to
  mirror :func:`app.storage.tags.create_tag`.
* :func:`untag_older_than` cuts ``screenshot_tags`` rows only — it
  never touches the underlying ``tags`` row, so the surviving recent
  shots keep their colour and friends.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.time import iso

log = get_logger("persona.tag.cleanup")


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


async def find_orphan_tags() -> list[str]:
    """Return the names of tags that have zero rows in ``screenshot_tags``.

    Result is sorted alphabetically so the CLI output stays stable
    between runs and is easy to diff. The query uses a ``LEFT JOIN``
    rather than a correlated ``NOT EXISTS`` because the
    ``idx_screenshot_tags_tag`` index makes the join cheap and the
    explicit ``st.tag_id IS NULL`` filter reads more obviously than the
    subquery form.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT t.name AS name "
            "FROM tags t "
            "LEFT JOIN screenshot_tags st ON st.tag_id = t.id "
            "WHERE st.tag_id IS NULL "
            "ORDER BY t.name"
        )
        rows = await cursor.fetchall()
    names = [str(row["name"]) for row in rows]
    log.info("tag_cleanup.find_orphans", count=len(names))
    return names


async def purge_orphan_tags() -> int:
    """Delete every tag with zero ``screenshot_tags`` rows, return how many.

    Runs inside a single transaction so an error rolls back to the
    pre-purge state. We re-compute the orphan set inside the
    transaction (rather than reusing :func:`find_orphan_tags`) so a
    concurrent tagging operation between the two calls cannot trick us
    into deleting a tag that just got its first screenshot.
    """
    async with get_connection() as conn:
        try:
            await conn.execute("BEGIN")
            cursor = await conn.execute(
                "SELECT t.id AS id "
                "FROM tags t "
                "LEFT JOIN screenshot_tags st ON st.tag_id = t.id "
                "WHERE st.tag_id IS NULL"
            )
            rows = await cursor.fetchall()
            ids = [int(row["id"]) for row in rows]
            for tag_id in ids:
                await conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
            await conn.commit()
        except aiosqlite.Error:
            await conn.rollback()
            log.exception("tag_cleanup.purge_failed")
            raise
    log.info("tag_cleanup.purge_orphans", deleted=len(ids))
    return len(ids)


async def untag_older_than(tag_name: str, days: int) -> int:
    """Remove ``tag_name`` from every screenshot older than ``days`` days.

    Parameters
    ----------
    tag_name:
        Tag to detach. Lookup is case-insensitive and whitespace-trimmed.
        If no tag matches, the call is a no-op and returns ``0``.
    days:
        How many days back the cutoff sits. ``days=30`` means "remove
        the tag from every shot captured before now-30d". Must be
        ``>= 0`` — a negative window is rejected with ``ValueError``.

    Returns
    -------
    int
        Number of ``screenshot_tags`` rows actually deleted.
    """
    if days < 0:
        msg = f"days must be >= 0, got {days}"
        raise ValueError(msg)

    name = _normalise(tag_name)
    if not name:
        log.warning("tag_cleanup.untag_old.empty_name")
        return 0

    cutoff = datetime.now(tz=UTC) - timedelta(days=days)
    cutoff_iso = iso(cutoff)

    async with get_connection() as conn:
        tag_id = await _lookup_tag_id(conn, name)
        if tag_id is None:
            log.info("tag_cleanup.untag_old.no_tag", tag=name, days=days)
            return 0

        try:
            await conn.execute("BEGIN")
            # Two-step: identify victims first so we can return an
            # accurate row count even on SQLite builds where
            # ``cursor.rowcount`` is unreliable on multi-row DELETE.
            cursor = await conn.execute(
                "SELECT st.screenshot_id AS sid "
                "FROM screenshot_tags st "
                "JOIN screenshots s ON s.id = st.screenshot_id "
                "WHERE st.tag_id = ? AND s.captured_at < ?",
                (tag_id, cutoff_iso),
            )
            victim_rows = await cursor.fetchall()
            victims = [int(row["sid"]) for row in victim_rows]
            for sid in victims:
                await conn.execute(
                    "DELETE FROM screenshot_tags "
                    "WHERE tag_id = ? AND screenshot_id = ?",
                    (tag_id, sid),
                )
            await conn.commit()
        except aiosqlite.Error:
            await conn.rollback()
            log.exception(
                "tag_cleanup.untag_old.failed",
                tag=name,
                days=days,
            )
            raise

    affected = len(victims)
    log.info(
        "tag_cleanup.untag_old.applied",
        tag=name,
        days=days,
        cutoff=cutoff_iso,
        affected=affected,
    )
    return affected


__all__ = [
    "find_orphan_tags",
    "purge_orphan_tags",
    "untag_older_than",
]
