"""Daily LLM digest scheduler (v1.31 — onto :class:`ClockScheduler`).

Fires once per day when the local-time hour matches
``auto_digest_hour_local`` AND a digest for "today" doesn't already
exist in ``daily_digest``. The clock-marker debounce in ClockScheduler
prevents accidental double-firing within the same hour; the existing
``SELECT FROM daily_digest`` check is kept as defence-in-depth.

Earlier implementation read ``settings.auto_digest_enabled`` ONCE at
startup, so flipping the toggle at runtime required a restart.
``ClockScheduler.enabled_getter`` is called on every tick, so the new
implementation picks up the change within the poll cadence.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from app.llm import LLMNotConfigured, summarise_day
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.workers._bases import ClockScheduler
from app.workers.control import CaptureController, get_controller

log = get_logger("persona.digest_scheduler")

POLL_INTERVAL_SECONDS = 600.0


async def _hour_getter() -> int:
    return int(get_settings().auto_digest_hour_local)


async def _enabled_getter() -> bool:
    return bool(get_settings().auto_digest_enabled)


async def _job() -> None:
    """Generate today's digest if it's not already in ``daily_digest``."""
    settings = get_settings()
    today_iso = datetime.now().astimezone().strftime("%Y-%m-%d")

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM daily_digest WHERE day = ?",
            (today_iso,),
        )
        if await cursor.fetchone() is not None:
            log.debug("digest_scheduler.skip_already_done", day=today_iso)
            return

    try:
        text = await summarise_day(datetime.now().astimezone())
    except LLMNotConfigured as exc:
        log.warning("digest_scheduler.no_llm", error=str(exc))
        return

    async with get_connection() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO daily_digest (day, body, provider) VALUES (?, ?, ?)",
            (today_iso, text, settings.byo_api_provider or None),
        )
        await conn.commit()

    log.info("digest_scheduler.generated", day=today_iso, chars=len(text))


async def run_digest_scheduler(controller: CaptureController | None = None) -> None:
    """Lifespan entry point."""
    ctrl = controller or get_controller()
    scheduler = ClockScheduler(
        name="digest-scheduler",
        hour_local_getter=_hour_getter,
        enabled_getter=_enabled_getter,
        marker_kv="digest_scheduler_last_fired",
        job=_job,
        poll_seconds=int(POLL_INTERVAL_SECONDS),
    )
    await scheduler.run(ctrl.stop_event)
