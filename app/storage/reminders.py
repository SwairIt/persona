"""Tiny single-day reminders — quick todos for "today only"."""

from __future__ import annotations

from datetime import date
from typing import Any

import aiosqlite


async def create_reminder(
    conn: aiosqlite.Connection,
    *,
    body: str,
    due_date: date,
    screenshot_id: int | None = None,
) -> int:
    cursor = await conn.execute(
        "INSERT INTO reminders (body, due_date, screenshot_id) VALUES (?, ?, ?)",
        (body, due_date.isoformat(), screenshot_id),
    )
    await conn.commit()
    if cursor.lastrowid is None:
        msg = "reminder insert returned no id"
        raise RuntimeError(msg)
    return int(cursor.lastrowid)


async def list_for_day(
    conn: aiosqlite.Connection,
    *,
    day: date,
    include_done: bool = True,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM reminders WHERE due_date = ?"
    params: list[Any] = [day.isoformat()]
    if not include_done:
        sql += " AND done = 0"
    sql += " ORDER BY done ASC, created_at ASC"
    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "body": str(row["body"]),
            "due_date": str(row["due_date"]),
            "done": bool(row["done"]),
            "created_at": str(row["created_at"]),
            "completed_at": row["completed_at"],
            "screenshot_id": (
                int(row["screenshot_id"]) if row["screenshot_id"] is not None else None
            ),
        }
        for row in rows
    ]


async def list_pending_anywhere(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    """Reminders with due_date <= today that are still not done."""
    cursor = await conn.execute(
        "SELECT * FROM reminders WHERE done = 0 AND due_date <= DATE('now') "
        "ORDER BY due_date ASC, created_at ASC"
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "body": str(row["body"]),
            "due_date": str(row["due_date"]),
            "done": bool(row["done"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def list_for_screenshot(
    conn: aiosqlite.Connection,
    screenshot_id: int,
) -> list[dict[str, Any]]:
    """All reminders attached to a given screenshot (any due_date)."""
    cursor = await conn.execute(
        "SELECT * FROM reminders WHERE screenshot_id = ? "
        "ORDER BY done ASC, due_date ASC, created_at ASC",
        (screenshot_id,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "body": str(row["body"]),
            "due_date": str(row["due_date"]),
            "done": bool(row["done"]),
            "created_at": str(row["created_at"]),
            "completed_at": row["completed_at"],
            "screenshot_id": (
                int(row["screenshot_id"]) if row["screenshot_id"] is not None else None
            ),
        }
        for row in rows
    ]


async def toggle_done(conn: aiosqlite.Connection, reminder_id: int, done: bool) -> None:
    if done:
        await conn.execute(
            "UPDATE reminders SET done = 1, completed_at = datetime('now') WHERE id = ?",
            (reminder_id,),
        )
    else:
        await conn.execute(
            "UPDATE reminders SET done = 0, completed_at = NULL WHERE id = ?",
            (reminder_id,),
        )
    await conn.commit()


async def delete_reminder(conn: aiosqlite.Connection, reminder_id: int) -> None:
    await conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
    await conn.commit()
