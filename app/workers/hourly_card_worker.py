"""Hourly card builder worker (v1.14).

Wakes up every ~10 minutes, checks the clock, and writes a tier-1 card
for any completed hour that doesn't have one yet. Catches up on missed
hours after a long uptime gap (e.g. the laptop was closed for 4 hours
— next tick generates four cards in one pass).

Heuristic-only — no LLM call. The deterministic version of the card
is shipped immediately; LLM enrichment is a separate optional pass.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.hourly_card import build_card_for_hour
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.workers.heartbeat import beat

log = get_logger("persona.hourly_card_worker")

# Poll cadence. 10 minutes is enough — cards are bucketed by hour, so a
# slower tick than that just means a few extra minutes of latency.
POLL_INTERVAL_SECONDS: int = 600

# How far back to look on each tick. Default 24 h covers gap-after-sleep
# without forcing a full-history backfill (that's a separate one-shot CLI).
LOOKBACK_HOURS: int = 24


async def _hours_to_build(now: datetime) -> list[datetime]:
    """Return list of hour_start datetimes that need a card written.

    Each hour in the lookback window is included if it (a) is fully in
    the past (we never write a card for the in-progress hour) and (b)
    isn't already present in ``hourly_card``.
    """
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    candidates: list[datetime] = []
    for offset in range(1, LOOKBACK_HOURS + 1):
        h = current_hour - timedelta(hours=offset)
        candidates.append(h)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT hour_start FROM hourly_card "
            "WHERE hour_start >= ?",
            ((current_hour - timedelta(hours=LOOKBACK_HOURS)).isoformat(),),
        )
        rows = await cursor.fetchall()
    have = {str(r["hour_start"]) for r in rows}

    return [h for h in candidates if h.isoformat() not in have]


async def run_hourly_card_worker(stop_event: asyncio.Event | None = None) -> None:
    """Sleep loop that materialises one card per completed missing hour."""
    stop = stop_event or asyncio.Event()
    log.info("hourly_card_worker.started", lookback_h=LOOKBACK_HOURS)

    while not stop.is_set():
        await beat("hourly-card-worker")
        try:
            now = datetime.now(tz=UTC)
            missing = await _hours_to_build(now)
            built = 0
            for hour_start in missing:
                try:
                    result = await build_card_for_hour(hour_start)
                    if result is not None:
                        built += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "hourly_card_worker.build_failed",
                        hour=hour_start.isoformat(),
                        error=str(exc),
                    )
            if built:
                log.info("hourly_card_worker.cycle", built=built)
        except asyncio.CancelledError:
            log.info("hourly_card_worker.cancelled")
            raise
        except Exception as exc:
            log.exception("hourly_card_worker.iteration_failed", error=str(exc))

        try:
            await asyncio.wait_for(stop.wait(), timeout=POLL_INTERVAL_SECONDS)
        except TimeoutError:
            continue

    log.info("hourly_card_worker.stopped")


__all__ = ["run_hourly_card_worker"]
