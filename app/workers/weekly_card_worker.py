"""Weekly card writer (tier 2, v1.15, refactored onto BackfillRunner in v1.26).

Wakes every ~1 hour. Looks back 12 weeks and writes a tier-2 card for
any ISO week that doesn't have one yet and has at least one screenshot
or audio segment. Fast cadence so a laptop that was closed for a long
stretch backfills missing weeks promptly after coming back online.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

from app.storage.db import get_connection
from app.weekly_card import build_card_for_week
from app.workers._bases import BackfillRunner

POLL_INTERVAL_SECONDS: int = 3600
LOOKBACK_WEEKS: int = 12


def _monday_of(when: date) -> date:
    """Return the Monday of the ISO week containing ``when``."""
    return when - timedelta(days=when.weekday())


async def _weeks_to_build() -> list[date]:
    """Return Mondays in the lookback window that have no weekly_card yet."""
    today = datetime.now(tz=UTC).date()
    current_monday = _monday_of(today)
    candidates = [
        current_monday - timedelta(weeks=offset)
        for offset in range(1, LOOKBACK_WEEKS + 1)
    ]
    oldest = candidates[-1] if candidates else current_monday

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT week_start FROM weekly_card WHERE week_start >= ?",
            (oldest.isoformat(),),
        )
        rows = await cursor.fetchall()
    have = {str(r["week_start"]) for r in rows}

    return [w for w in candidates if w.isoformat() not in have]


async def run_weekly_card_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan entry point — registers a :class:`BackfillRunner`."""
    runner = BackfillRunner(
        name="weekly-card-worker",
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_weeks_to_build,
        build_one=build_card_for_week,
    )
    await runner.run(stop_event)


__all__ = ["run_weekly_card_worker"]
