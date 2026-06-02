"""Track daily thumbnail-bytes usage for the budget dashboard."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite

from app.storage.time import iso


async def sample_today(conn: aiosqlite.Connection, thumbnails_dir: Path) -> dict[str, int]:
    """Measure today's thumbnails bytes + count, upsert into daily_size_log."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    folder = thumbnails_dir / today

    total_bytes = 0
    total_files = 0
    if folder.exists():
        for path in folder.glob("*.webp"):
            try:
                total_bytes += path.stat().st_size
                total_files += 1
            except OSError:
                continue

    await conn.execute(
        """
        INSERT INTO daily_size_log (day, thumbnails_bytes, screenshot_count, sampled_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(day) DO UPDATE SET
            thumbnails_bytes = excluded.thumbnails_bytes,
            screenshot_count = excluded.screenshot_count,
            sampled_at = datetime('now')
        """,
        (today, total_bytes, total_files),
    )
    await conn.commit()
    return {"bytes": total_bytes, "files": total_files}


async def list_recent(
    conn: aiosqlite.Connection,
    *,
    days: int = 14,
) -> list[dict[str, object]]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cursor = await conn.execute(
        "SELECT day, thumbnails_bytes, screenshot_count FROM daily_size_log "
        "WHERE day >= ? ORDER BY day",
        (cutoff.strftime("%Y-%m-%d"),),
    )
    rows = await cursor.fetchall()
    return [
        {
            "day": str(row["day"]),
            "bytes": int(row["thumbnails_bytes"]),
            "screenshots": int(row["screenshot_count"]),
        }
        for row in rows
    ]


async def today_bytes(conn: aiosqlite.Connection) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cursor = await conn.execute(
        "SELECT thumbnails_bytes FROM daily_size_log WHERE day = ?",
        (today,),
    )
    row = await cursor.fetchone()
    return int(row["thumbnails_bytes"]) if row else 0


__all__ = ["list_recent", "sample_today", "today_bytes", "iso"]
