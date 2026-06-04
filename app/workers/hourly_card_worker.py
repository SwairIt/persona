"""Hourly card builder worker (v1.14, refactored onto BackfillRunner in v1.26).

Wakes up every ~10 minutes, checks the clock, and writes a tier-1 card
for any completed hour that doesn't have one yet. Catches up on missed
hours after a long uptime gap (e.g. the laptop was closed for 4 hours
— next tick generates four cards in one pass).

The loop / failure handling / heartbeat plumbing lives in
:class:`app.workers._bases.BackfillRunner`; this module only provides
the missing-hour lister + per-hour build callback.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.hourly_card import build_card_for_hour
from app.storage.db import get_connection
from app.workers._bases import BackfillRunner

POLL_INTERVAL_SECONDS: int = 600
LOOKBACK_HOURS: int = 24


async def _hours_to_build() -> list[datetime]:
    """Return list of hour_start datetimes that need a card written."""
    now = datetime.now(tz=UTC)
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    candidates = [
        current_hour - timedelta(hours=offset)
        for offset in range(1, LOOKBACK_HOURS + 1)
    ]

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT hour_start FROM hourly_card WHERE hour_start >= ?",
            ((current_hour - timedelta(hours=LOOKBACK_HOURS)).isoformat(),),
        )
        rows = await cursor.fetchall()
    have = {str(r["hour_start"]) for r in rows}
    return [h for h in candidates if h.isoformat() not in have]


async def run_hourly_card_worker(stop_event: asyncio.Event | None = None) -> None:
    """Lifespan entry point — registers a :class:`BackfillRunner`."""
    runner = BackfillRunner(
        name="hourly-card-worker",
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_hours_to_build,
        build_one=build_card_for_hour,
    )
    await runner.run(stop_event)


__all__ = ["run_hourly_card_worker"]
