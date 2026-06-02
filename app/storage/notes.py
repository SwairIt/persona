"""CRUD for free-text notes attached to screenshots, plus standalone notes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.storage.tags import create_tag

if TYPE_CHECKING:
    import aiosqlite


async def insert_inbox_note(
    conn: aiosqlite.Connection,
    *,
    body: str,
    title: str | None = None,
    source: str | None = None,
) -> int:
    """Insert a standalone markdown note (no screenshot link).

    Returns the new ``notes.id``. Commits immediately so the caller can
    move the source file out of the inbox knowing the row is durable.
    """
    cursor = await conn.execute(
        "INSERT INTO notes (title, body, source) VALUES (?, ?, ?)",
        (title, body, source),
    )
    await conn.commit()
    row_id = cursor.lastrowid
    if row_id is None:
        msg = "INSERT INTO notes did not return a row id"
        raise RuntimeError(msg)
    return int(row_id)


async def add_tag(
    conn: aiosqlite.Connection,
    note_id: int,
    tag_name: str,
) -> int:
    """Attach a tag to a standalone note. Creates the tag row if missing.

    Returns the ``tags.id`` of the (possibly freshly created) tag. The
    ``note_tags`` insert is ``INSERT OR IGNORE`` so calling this twice
    with the same pair is a no-op — keeping the inbox worker idempotent
    when the same file is re-imported by hand.
    """
    cleaned = (tag_name or "").strip().lower()
    if not cleaned:
        msg = "tag name is required"
        raise ValueError(msg)
    tag_id = await create_tag(conn, name=cleaned)
    await conn.execute(
        "INSERT OR IGNORE INTO note_tags (note_id, tag_id) VALUES (?, ?)",
        (note_id, tag_id),
    )
    await conn.commit()
    return tag_id


async def list_recent_inbox_notes(
    conn: aiosqlite.Connection,
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Most-recent standalone notes, newest first — for the inbox status page."""
    cursor = await conn.execute(
        """
        SELECT id, title, body, source, created_at
        FROM notes
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(limit),),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "title": (str(row["title"]) if row["title"] is not None else None),
            "body": str(row["body"]),
            "source": (str(row["source"]) if row["source"] is not None else None),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def get_note(conn: aiosqlite.Connection, screenshot_id: int) -> str | None:
    cursor = await conn.execute(
        "SELECT body FROM screenshot_notes WHERE screenshot_id = ?",
        (screenshot_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return str(row["body"])


async def upsert_note(
    conn: aiosqlite.Connection,
    screenshot_id: int,
    body: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO screenshot_notes (screenshot_id, body)
        VALUES (?, ?)
        ON CONFLICT(screenshot_id) DO UPDATE SET
            body = excluded.body,
            updated_at = datetime('now')
        """,
        (screenshot_id, body),
    )
    await conn.commit()


async def delete_note(conn: aiosqlite.Connection, screenshot_id: int) -> None:
    await conn.execute(
        "DELETE FROM screenshot_notes WHERE screenshot_id = ?",
        (screenshot_id,),
    )
    await conn.commit()
