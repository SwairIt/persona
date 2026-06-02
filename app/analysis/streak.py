"""Consecutive-day streak metrics — how many days in a row you captured anything."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

import aiosqlite


@dataclass(frozen=True, slots=True)
class StreakSummary:
    current_streak: int
    longest_streak: int
    active_days_30d: int
    active_days_total: int


async def compute_streaks(conn: aiosqlite.Connection, *, today: date | None = None) -> StreakSummary:
    today_local = today or datetime.now().astimezone().date()
    cursor = await conn.execute(
        "SELECT DISTINCT DATE(captured_at) AS day FROM screenshots ORDER BY day"
    )
    rows = await cursor.fetchall()
    days = sorted({_parse_day(str(row["day"])) for row in rows if row["day"]})
    if not days:
        return StreakSummary(0, 0, 0, 0)

    days_set = set(days)

    cutoff_30 = today_local - timedelta(days=29)
    active_30 = sum(1 for d in days if d >= cutoff_30)

    longest = 0
    run = 0
    previous: date | None = None
    for d in days:
        if previous is None or d == previous + timedelta(days=1):
            run += 1
        else:
            run = 1
        longest = max(longest, run)
        previous = d

    current = 0
    cursor_day = today_local
    if cursor_day not in days_set:
        cursor_day = today_local - timedelta(days=1)
    while cursor_day in days_set:
        current += 1
        cursor_day -= timedelta(days=1)

    return StreakSummary(
        current_streak=current,
        longest_streak=longest,
        active_days_30d=active_30,
        active_days_total=len(days),
    )


def _parse_day(value: str) -> date:
    return date.fromisoformat(value[:10])
