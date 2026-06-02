"""Storage-tier helpers — promote / demote screenshots between hot / warm / cold."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

import aiosqlite

Tier = Literal["hot", "warm", "cold", "pinned"]


async def set_tier(
    conn: aiosqlite.Connection,
    screenshot_id: int,
    tier: Tier,
) -> None:
    await conn.execute(
        "UPDATE screenshots SET tier = ? WHERE id = ?",
        (tier, screenshot_id),
    )
    await conn.commit()


async def list_by_tier(
    conn: aiosqlite.Connection,
    tier: Tier,
    *,
    until: datetime | None = None,
    limit: int = 200,
) -> list[dict[str, object]]:
    """Return screenshots in the given tier (optionally older than `until`)."""
    from app.storage.time import iso

    params: list[object] = [tier]
    sql = "SELECT id, captured_at, thumbnail_path FROM screenshots WHERE tier = ?"
    if until is not None:
        sql += " AND captured_at < ?"
        params.append(iso(until))
    sql += " ORDER BY captured_at ASC LIMIT ?"
    params.append(limit)

    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "captured_at": str(row["captured_at"]),
            "thumbnail_path": row["thumbnail_path"],
        }
        for row in rows
    ]


async def count_by_tier(conn: aiosqlite.Connection) -> dict[str, int]:
    cursor = await conn.execute(
        "SELECT tier, COUNT(*) AS n FROM screenshots GROUP BY tier"
    )
    rows = await cursor.fetchall()
    return {str(row["tier"]): int(row["n"]) for row in rows}


async def pin_screenshot(conn: aiosqlite.Connection, screenshot_id: int) -> None:
    await set_tier(conn, screenshot_id, "pinned")


async def unpin_screenshot(conn: aiosqlite.Connection, screenshot_id: int) -> None:
    """Move pinned back to hot. The next tier-sweep will move it to warm/cold."""
    await set_tier(conn, screenshot_id, "hot")
