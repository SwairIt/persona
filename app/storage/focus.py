"""CRUD for focus sessions (Pomodoro-style)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import aiosqlite

from app.storage.time import iso, parse_iso


async def start_session(
    conn: aiosqlite.Connection,
    *,
    started_at: datetime,
    duration_minutes: int,
    intent: str | None,
) -> int:
    cursor = await conn.execute(
        "INSERT INTO focus_sessions (started_at, duration_minutes, intent) VALUES (?, ?, ?)",
        (iso(started_at), duration_minutes, intent),
    )
    await conn.commit()
    if cursor.lastrowid is None:
        msg = "focus insert returned no id"
        raise RuntimeError(msg)
    return int(cursor.lastrowid)


async def finish_session(
    conn: aiosqlite.Connection,
    session_id: int,
    *,
    ended_at: datetime,
    completed: bool,
    outcome: str | None = None,
) -> None:
    await conn.execute(
        "UPDATE focus_sessions SET ended_at = ?, completed = ?, outcome = ? WHERE id = ?",
        (iso(ended_at), 1 if completed else 0, outcome, session_id),
    )
    await conn.commit()


async def list_recent_sessions(
    conn: aiosqlite.Connection,
    *,
    limit: int = 30,
) -> list[dict[str, Any]]:
    cursor = await conn.execute(
        "SELECT id, started_at, ended_at, duration_minutes, intent, outcome, completed "
        "FROM focus_sessions ORDER BY started_at DESC LIMIT ?",
        (limit,),
    )
    rows = await cursor.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        out.append(
            {
                "id": int(row["id"]),
                "started_at": parse_iso(str(row["started_at"])),
                "ended_at": parse_iso(str(row["ended_at"])) if row["ended_at"] else None,
                "duration_minutes": int(row["duration_minutes"]),
                "intent": row["intent"],
                "outcome": row["outcome"],
                "completed": bool(row["completed"]),
            }
        )
    return out


async def session_count_today(conn: aiosqlite.Connection, today: str) -> int:
    cursor = await conn.execute(
        "SELECT COUNT(*) AS n FROM focus_sessions "
        "WHERE DATE(started_at) = ? AND completed = 1",
        (today,),
    )
    row = await cursor.fetchone()
    return int(row["n"]) if row else 0
