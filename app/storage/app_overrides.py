"""User-defined per-app capture-interval overrides.

Some apps deserve a denser capture cadence (e.g. Slack), others sparser
(e.g. Spotify). The capture loop consults this table at runtime to pick
the next sleep interval.

App names are stored verbatim (case-sensitive) so they match the same way
the capture loop persists them on the screenshot row.
"""

from __future__ import annotations

from typing import Any

import aiosqlite

_MIN_INTERVAL = 0.5
_MAX_INTERVAL = 60.0


async def list_overrides(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT app_name, interval_seconds, created_at FROM app_capture_overrides "
        "ORDER BY app_name"
    )
    rows = await cursor.fetchall()
    return [
        {
            "app_name": str(row["app_name"]),
            "interval_seconds": float(row["interval_seconds"]),
            "created_at": str(row["created_at"]),
        }
        for row in rows
    ]


async def upsert_override(
    conn: aiosqlite.Connection,
    *,
    app_name: str,
    interval_seconds: float,
) -> None:
    app_name = app_name.strip()
    if not app_name:
        msg = "app_name is required"
        raise ValueError(msg)
    if not (_MIN_INTERVAL <= interval_seconds <= _MAX_INTERVAL):
        msg = (
            f"interval_seconds must be between {_MIN_INTERVAL} and "
            f"{_MAX_INTERVAL}, got {interval_seconds}"
        )
        raise ValueError(msg)
    await conn.execute(
        """
        INSERT INTO app_capture_overrides (app_name, interval_seconds)
        VALUES (?, ?)
        ON CONFLICT(app_name) DO UPDATE SET interval_seconds = excluded.interval_seconds
        """,
        (app_name, float(interval_seconds)),
    )
    await conn.commit()


async def delete_override(conn: aiosqlite.Connection, app_name: str) -> None:
    await conn.execute(
        "DELETE FROM app_capture_overrides WHERE app_name = ?",
        (app_name.strip(),),
    )
    await conn.commit()


async def lookup_override(
    conn: aiosqlite.Connection,
    app_name: str,
) -> float | None:
    cursor = await conn.execute(
        "SELECT interval_seconds FROM app_capture_overrides WHERE app_name = ?",
        (app_name.strip(),),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    return float(row["interval_seconds"])
