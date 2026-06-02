"""CRUD for per-screenshot free-form annotations.

Annotations are append-only commentary tied to a single screenshot. They are
deliberately distinct from:

* ``screenshot_notes`` (one global note per screenshot, mutable, FTS-indexed);
* tag attachments (controlled vocabulary).

Each helper opens nothing — callers pass an :class:`aiosqlite.Connection`
obtained from :func:`app.storage.db.get_connection`. Parameters are always
bound via ``?`` placeholders to keep SQL injection off the table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.annotations")


def _row_to_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "screenshot_id": int(row["screenshot_id"]),
        "body": str(row["body"]),
        "created_at": str(row["created_at"]),
    }


async def list_for_screenshot(
    conn: aiosqlite.Connection,
    shot_id: int,
) -> list[dict[str, Any]]:
    """Return every annotation for the given screenshot, oldest first."""
    cursor = await conn.execute(
        "SELECT id, screenshot_id, body, created_at "
        "FROM screenshot_annotation "
        "WHERE screenshot_id = ? "
        "ORDER BY id ASC",
        (shot_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_dict(row) for row in rows]


async def add(
    conn: aiosqlite.Connection,
    shot_id: int,
    body: str,
) -> dict[str, Any]:
    """Insert a new annotation row and return it.

    The caller is responsible for validating ``body`` is non-empty; this
    function still defends with an assert to avoid silently writing blanks.
    """
    text = (body or "").strip()
    if not text:
        msg = "annotation body must not be empty"
        raise ValueError(msg)

    cursor = await conn.execute(
        "INSERT INTO screenshot_annotation (screenshot_id, body) VALUES (?, ?)",
        (shot_id, text),
    )
    row_id = cursor.lastrowid
    if row_id is None:
        msg = "INSERT did not return a row id"
        raise RuntimeError(msg)
    await conn.commit()

    cursor = await conn.execute(
        "SELECT id, screenshot_id, body, created_at "
        "FROM screenshot_annotation WHERE id = ?",
        (row_id,),
    )
    row = await cursor.fetchone()
    if row is None:  # pragma: no cover — sanity guard, INSERT just succeeded
        msg = f"annotation #{row_id} vanished immediately after insert"
        raise RuntimeError(msg)

    log.info("annotations.added", annotation_id=row_id, screenshot_id=shot_id)
    return _row_to_dict(row)


async def delete(conn: aiosqlite.Connection, ann_id: int) -> bool:
    """Delete the annotation; return True if a row was removed, False otherwise."""
    cursor = await conn.execute(
        "DELETE FROM screenshot_annotation WHERE id = ?",
        (ann_id,),
    )
    await conn.commit()
    removed = (cursor.rowcount or 0) > 0
    log.info("annotations.deleted", annotation_id=ann_id, removed=removed)
    return removed
