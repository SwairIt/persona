"""Daily pin writer (tier 5, v1.14).

Wakes every ~30 minutes. After 00:10 UTC writes yesterday's pin if it
isn't already there. After a long uptime gap, fills any missed days
inside a 30-day lookback so a 2-week laptop-closed period catches up
in one pass.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

from app.daily_pin import write_pin_for_day
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.workers.heartbeat import beat

log = get_logger("persona.daily_pin_worker")

POLL_INTERVAL_SECONDS: int = 1800
LOOKBACK_DAYS: int = 30


async def _days_to_pin(now: datetime) -> list[date]:
    """Return list of (yesterday → 30 days back) that have no pin yet."""
    today = now.date()
    candidates = [today - timedelta(days=offset) for offset in range(1, LOOKBACK_DAYS + 1)]

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT day FROM daily_pin WHERE day >= ?",
            ((today - timedelta(days=LOOKBACK_DAYS)).isoformat(),),
        )
        rows = await cursor.fetchall()
    have = {str(r["day"]) for r in rows}

    return [d for d in candidates if d.isoformat() not in have]


async def run_daily_pin_worker(stop_event: asyncio.Event | None = None) -> None:
    stop = stop_event or asyncio.Event()
    log.info("daily_pin_worker.started", lookback_d=LOOKBACK_DAYS)

    while not stop.is_set():
        await beat("daily-pin-worker")
        try:
            now = datetime.now(tz=UTC)
            missing = await _days_to_pin(now)
            written = 0
            for d in missing:
                try:
                    res = await write_pin_for_day(d)
                    if res is not None:
                        written += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "daily_pin_worker.write_failed",
                        day=d.isoformat(),
                        error=str(exc),
                    )
            if written:
                log.info("daily_pin_worker.cycle", written=written)
        except asyncio.CancelledError:
            log.info("daily_pin_worker.cancelled")
            raise
        except Exception as exc:
            log.exception("daily_pin_worker.iteration_failed", error=str(exc))

        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_SECONDS)
        except TimeoutError:
            continue

    log.info("daily_pin_worker.stopped")


__all__ = ["run_daily_pin_worker"]
