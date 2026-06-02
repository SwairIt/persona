"""User-defined process-name → app-name remapping.

Lives next to `app.capture.window._derive_app_name`'s built-in table; user
overrides take precedence at capture time.
"""

from __future__ import annotations

from typing import Any

import aiosqlite


async def list_remaps(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT process_name, app_name, created_at FROM process_app_remap "
        "ORDER BY process_name"
    )
    rows = await cursor.fetchall()
    return [
        {
            "process_name": str(row["process_name"]),
            "app_name": str(row["app_name"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def upsert_remap(
    conn: aiosqlite.Connection,
    *,
    process_name: str,
    app_name: str,
) -> None:
    process_name = process_name.strip().lower()
    app_name = app_name.strip()
    if not process_name or not app_name:
        msg = "Both process_name and app_name are required"
        raise ValueError(msg)
    await conn.execute(
        """
        INSERT INTO process_app_remap (process_name, app_name)
        VALUES (?, ?)
        ON CONFLICT(process_name) DO UPDATE SET app_name = excluded.app_name
        """,
        (process_name, app_name),
    )
    await conn.commit()


async def delete_remap(conn: aiosqlite.Connection, process_name: str) -> None:
    await conn.execute(
        "DELETE FROM process_app_remap WHERE process_name = ?",
        (process_name.strip().lower(),),
    )
    await conn.commit()


async def lookup_remap(
    conn: aiosqlite.Connection,
    process_name: str,
) -> str | None:
    cursor = await conn.execute(
        "SELECT app_name FROM process_app_remap WHERE process_name = ?",
        (process_name.strip().lower(),),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return str(row["app_name"])
