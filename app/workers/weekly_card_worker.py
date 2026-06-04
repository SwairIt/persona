"""Weekly card writer (tier 2, v1.15).

Wakes every ~1 hour. Looks back 12 weeks and writes a tier-2 card for
any ISO week that doesn't have one yet and has at least one screenshot
or audio segment. Fast cadence so a laptop that was closed for a long
stretch backfills missing weeks promptly after coming back online.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.weekly_card import build_card_for_week
from app.workers.heartbeat import beat

log = get_logger("persona.weekly_card_worker")

POLL_INTERVAL_SECONDS: int = 3600
LOOKBACK_WEEKS: int = 12


def _monday_of(when: date) -> date:
    """Return the Monday of the ISO week containing ``when``."""
    return when - timedelta(days=when.weekday())


async def _weeks_to_build(now: datetime) -> list[date]:
    """Return Mondays in the lookback window that have no weekly_card yet."""
    today = now.date()
    current_monday = _monday_of(today)
    # Skip the in-progress week — start at last week, go LOOKBACK_WEEKS back.
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
    """Sleep loop that materialises one card per missing week."""
    stop = stop_event or asyncio.Event()
    log.info("weekly_card_worker.started", lookback_weeks=LOOKBACK_WEEKS)

    while not stop.is_set():
        await beat("weekly-card-worker")
        try:
            now = datetime.now(tz=UTC)
            missing = await _weeks_to_build(now)
            built = 0
            for week_start in missing:
                try:
                    result = await build_card_for_week(week_start)
                    if result is not None:
                        built += 1
                except Exception as exc:
                    log.warning(
                        "weekly_card_worker.build_failed",
                        week_start=week_start.isoformat(),
                        error=str(exc),
                    )
            if built:
                log.info("weekly_card_worker.cycle", built=built)
        except asyncio.CancelledError:
            log.info("weekly_card_worker.cancelled")
            raise
        except Exception as exc:
            log.exception("weekly_card_worker.iteration_failed", error=str(exc))

        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_SECONDS)
        except TimeoutError:
            continue

    log.info("weekly_card_worker.stopped")


__all__ = ["run_weekly_card_worker"]
