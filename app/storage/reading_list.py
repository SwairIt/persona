"""Reading list — save screenshots for later, mark when read."""

from __future__ import annotations

import aiosqlite

from app.storage.models import Screenshot
from app.storage.repository import _row_to_screenshot  # type: ignore[attr-defined]


async def add_to_reading_list(conn: aiosqlite.Connection, screenshot_id: int) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO reading_list (screenshot_id) VALUES (?)",
        (screenshot_id,),
    )
    await conn.commit()


async def remove_from_reading_list(conn: aiosqlite.Connection, screenshot_id: int) -> None:
    await conn.execute(
        "DELETE FROM reading_list WHERE screenshot_id = ?",
        (screenshot_id,),
    )
    await conn.commit()


async def mark_read(conn: aiosqlite.Connection, screenshot_id: int) -> None:
    await conn.execute(
        "UPDATE reading_list SET read_at = datetime('now') WHERE screenshot_id = ?",
        (screenshot_id,),
    )
    await conn.commit()


async def is_in_reading_list(conn: aiosqlite.Connection, screenshot_id: int) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM reading_list WHERE screenshot_id = ?",
        (screenshot_id,),
    )
    return await cursor.fetchone() is not None


async def list_reading_list(
    conn: aiosqlite.Connection,
    *,
    include_read: bool = False,
    limit: int = 200,
) -> list[Screenshot]:
    sql = (
        "SELECT s.* FROM screenshots s "
        "JOIN reading_list r ON r.screenshot_id = s.id"
    )
    if not include_read:
        sql += " WHERE r.read_at IS NULL"
    sql += " ORDER BY r.added_at DESC LIMIT ?"
    cursor = await conn.execute(sql, (limit,))
    rows = await cursor.fetchall()
    return [_row_to_screenshot(row) for row in rows]


async def count_unread(conn: aiosqlite.Connection) -> int:
    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM reading_list WHERE read_at IS NULL"
    )
    row = await cursor.fetchone()
    return int(row["n"]) if row else 0
