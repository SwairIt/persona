"""Search history — record recent queries, surface them as quick-picks."""

from __future__ import annotations

from typing import Any

import aiosqlite

_MAX_ROWS = 50


async def record_query(
    conn: aiosqlite.Connection,
    *,
    query: str,
    mode: str,
) -> None:
    """Record a query — bump use_count if it already exists, trim to last 50."""
    normalized = query.strip()
    if not normalized:
        return

    await conn.execute(
        """
        INSERT INTO search_history (query, mode, last_used_at, use_count)
        VALUES (?, ?, datetime('now'), 1)
        ON CONFLICT(query) DO UPDATE SET
            mode = excluded.mode,
            last_used_at = datetime('now'),
            use_count = search_history.use_count + 1
        """,
        (normalized, mode),
    )

    await conn.execute(
        """
        DELETE FROM search_history
        WHERE query NOT IN (
            SELECT query FROM search_history
            ORDER BY last_used_at DESC
            LIMIT ?
        )
        """,
        (_MAX_ROWS,),
    )
    await conn.commit()


async def list_recent(
    conn: aiosqlite.Connection,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return the N most-recent queries (newest first)."""
    cursor = await conn.execute(
        """
        SELECT query, mode, use_count, last_used_at
        FROM search_history
        ORDER BY last_used_at DESC
        LIMIT ?
        """,
        (limit,),
    )
    rows = await cursor.fetchall()
    return [
        {
            "query": str(row["query"]),
            "mode": str(row["mode"]),
            "use_count": int(row["use_count"]),
            "last_used_at": str(row["last_used_at"]),
        }
        for row in rows
    ]


async def clear_history(conn: aiosqlite.Connection) -> int:
    """Wipe everything, return count deleted."""
    cursor = await conn.execute("SELECT COUNT(*) AS n FROM search_history")
    row = await cursor.fetchone()
    deleted = int(row["n"]) if row is not None else 0

    await conn.execute("DELETE FROM search_history")
    await conn.commit()
    return deleted
