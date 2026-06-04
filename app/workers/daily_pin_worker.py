"""Daily pin writer (tier 5, v1.14, refactored onto BackfillRunner in v1.26).

Wakes every ~30 minutes. After 00:10 UTC writes yesterday's pin if it
isn't already there. After a long uptime gap, fills any missed days
inside a 30-day lookback so a 2-week laptop-closed period catches up
in one pass.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

from app.daily_pin import write_pin_for_day
from app.storage.db import get_connection
from app.workers._bases import BackfillRunner

POLL_INTERVAL_SECONDS: int = 1800
LOOKBACK_DAYS: int = 30


async def _days_to_pin() -> list[date]:
    """Return list of (yesterday → 30 days back) that have no pin yet."""
    today = datetime.now(tz=UTC).date()
    candidates = [
        today - timedelta(days=offset) for offset in range(1, LOOKBACK_DAYS + 1)
    ]

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT day FROM daily_pin WHERE day >= ?",
            ((today - timedelta(days=LOOKBACK_DAYS)).isoformat(),),
        )
        rows = await cursor.fetchall()
    have = {str(r["day"]) for r in rows}

    return [d for d in candidates if d.isoformat() not in have]


async def run_daily_pin_worker(stop_event: asyncio.Event | None = None) -> None:
    """Lifespan entry point — registers a :class:`BackfillRunner`."""
    runner = BackfillRunner(
        name="daily-pin-worker",
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_days_to_pin,
        build_one=write_pin_for_day,
    )
    await runner.run(stop_event)


__all__ = ["run_daily_pin_worker"]
