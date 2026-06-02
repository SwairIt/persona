"""Per-app active-minutes time-sheet.

Definition: two consecutive screenshots of the same app within `idle_gap`
seconds count toward that app's active time. A gap longer than `idle_gap`
breaks the session; we attribute only the gap-bounded interval.

For a single screenshot with no neighbour we attribute `capture_interval`
seconds (the configured tick), to avoid divide-by-zero on short days.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

import aiosqlite

from app.storage.time import iso

DEFAULT_IDLE_GAP_SECONDS = 300
DEFAULT_TICK_SECONDS = 5


@dataclass(frozen=True, slots=True)
class AppMinutes:
    app_name: str
    seconds: int


async def compute_per_app_seconds(
    conn: aiosqlite.Connection,
    *,
    day: date,
    idle_gap_seconds: int = DEFAULT_IDLE_GAP_SECONDS,
    tick_seconds: int = DEFAULT_TICK_SECONDS,
) -> list[AppMinutes]:
    """Return per-app total active seconds for the given local date."""
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)

    cursor = await conn.execute(
        "SELECT app_name, captured_at FROM screenshots "
        "WHERE captured_at >= ? AND captured_at < ? "
        "AND app_name IS NOT NULL "
        "ORDER BY app_name, captured_at",
        (iso(start), iso(end)),
    )
    rows = await cursor.fetchall()

    totals: dict[str, int] = {}
    prev_app: str | None = None
    prev_dt: datetime | None = None

    for row in rows:
        app = str(row["app_name"])
        when = datetime.fromisoformat(str(row["captured_at"]))
        if prev_app == app and prev_dt is not None:
            gap = (when - prev_dt).total_seconds()
            if 0 < gap <= idle_gap_seconds:
                totals[app] = totals.get(app, 0) + int(gap)
            else:
                totals[app] = totals.get(app, 0) + tick_seconds
        else:
            totals[app] = totals.get(app, 0) + tick_seconds
        prev_app = app
        prev_dt = when

    items = [AppMinutes(app_name=app, seconds=sec) for app, sec in totals.items()]
    items.sort(key=lambda r: r.seconds, reverse=True)
    return items


async def per_day_total_seconds(
    conn: aiosqlite.Connection,
    *,
    days: int = 365,
    idle_gap_seconds: int = DEFAULT_IDLE_GAP_SECONDS,
    tick_seconds: int = DEFAULT_TICK_SECONDS,
) -> dict[str, int]:
    """Total active seconds per day across the last `days` days. For year heatmap."""
    today = datetime.now().astimezone().date()
    cutoff = today - timedelta(days=days - 1)

    cursor = await conn.execute(
        "SELECT DATE(captured_at) AS day, captured_at FROM screenshots "
        "WHERE captured_at >= ? ORDER BY captured_at",
        (datetime.combine(cutoff, datetime.min.time()).isoformat(),),
    )
    rows = await cursor.fetchall()

    totals: dict[str, int] = {}
    prev_dt: datetime | None = None
    prev_day: str | None = None

    for row in rows:
        when = datetime.fromisoformat(str(row["captured_at"]))
        day_key = str(row["day"])
        if prev_dt is not None and prev_day == day_key:
            gap = (when - prev_dt).total_seconds()
            if 0 < gap <= idle_gap_seconds:
                totals[day_key] = totals.get(day_key, 0) + int(gap)
            else:
                totals[day_key] = totals.get(day_key, 0) + tick_seconds
        else:
            totals[day_key] = totals.get(day_key, 0) + tick_seconds
        prev_dt = when
        prev_day = day_key

    return totals


def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rem = minutes % 60
    if rem == 0:
        return f"{hours}h"
    return f"{hours}h {rem}m"
