"""Quiet-hours schedule — recurring weekly windows that auto-pause capture.

A rule covers a single weekday and a contiguous hour-of-day window
``[start_hour, end_hour)``. ``end_hour == 24`` means "until midnight".
Multiple rules can exist per weekday; the capture loop is paused when
any rule matches.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import aiosqlite


async def list_rules(conn: aiosqlite.Connection) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT id, weekday, start_hour, end_hour, label, created_at "
        "FROM quiet_hours ORDER BY weekday, start_hour"
    )
    rows = await cursor.fetchall()
    return [
        {
            "id": int(row["id"]),
            "weekday": int(row["weekday"]),
            "start_hour": int(row["start_hour"]),
            "end_hour": int(row["end_hour"]),
            "label": row["label"],
        }
        for row in rows
    ]


async def create_rule(
    conn: aiosqlite.Connection,
    *,
    weekday: int,
    start_hour: int,
    end_hour: int,
    label: str | None,
) -> int:
    if not (0 <= weekday <= 6):
        msg = f"weekday must be 0..6, got {weekday}"
        raise ValueError(msg)
    if not (0 <= start_hour < 24):
        msg = f"start_hour must be 0..23, got {start_hour}"
        raise ValueError(msg)
    if not (start_hour < end_hour <= 24):
        msg = (
            f"end_hour must be greater than start_hour and at most 24, "
            f"got start={start_hour}, end={end_hour}"
        )
        raise ValueError(msg)
    clean_label = label.strip() if label and label.strip() else None
    cursor = await conn.execute(
        "INSERT INTO quiet_hours (weekday, start_hour, end_hour, label) "
        "VALUES (?, ?, ?, ?)",
        (weekday, start_hour, end_hour, clean_label),
    )
    await conn.commit()
    if cursor.lastrowid is None:
        msg = "quiet_hours insert returned no id"
        raise RuntimeError(msg)
    return int(cursor.lastrowid)


async def delete_rule(conn: aiosqlite.Connection, rule_id: int) -> None:
    await conn.execute("DELETE FROM quiet_hours WHERE id = ?", (rule_id,))
    await conn.commit()


async def is_quiet_now(
    conn: aiosqlite.Connection,
    *,
    now: datetime | None = None,
) -> bool:
    """Return True if the local wall-clock falls inside any active rule.

    The comparison is done against ``now`` interpreted in the local
    timezone (``datetime.now().astimezone()`` when not provided).
    """
    moment = now if now is not None else datetime.now().astimezone()
    weekday = moment.weekday()
    hour = moment.hour
    cursor = await conn.execute(
        "SELECT 1 FROM quiet_hours "
        "WHERE weekday = ? AND start_hour <= ? AND end_hour > ? LIMIT 1",
        (weekday, hour, hour),
    )
    row = await cursor.fetchone()
    return row is not None
