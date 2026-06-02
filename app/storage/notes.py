"""CRUD for free-text notes attached to screenshots."""

from __future__ import annotations

import aiosqlite


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
