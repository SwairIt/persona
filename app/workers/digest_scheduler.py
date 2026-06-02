"""Once-a-day worker that generates and stores the daily LLM summary.

Runs when local-time hour matches `auto_digest_hour_local` and we haven't
already generated a digest for today. Cheap polling (every 10 min). No
extra dependency vs Summary route — just BYO LLM key.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.llm import LLMNotConfigured, summarise_day
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

log = get_logger("persona.digest_scheduler")

POLL_INTERVAL_SECONDS = 600.0


async def run_digest_scheduler(controller: CaptureController | None = None) -> None:
    ctrl = controller or get_controller()
    settings = get_settings()

    if not settings.auto_digest_enabled:
        log.info("digest_scheduler.disabled")
        await ctrl.stop_event.wait()
        return

    log.info("digest_scheduler.started", hour=settings.auto_digest_hour_local)

    while not ctrl.stop_event.is_set():
        await beat("digest-scheduler")
        try:
            await _maybe_generate()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("digest_scheduler.failed", error=str(exc))

        try:
            await asyncio.wait_for(ctrl.stop_event.wait(), timeout=POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            continue


async def _maybe_generate() -> None:
    settings = get_settings()
    now_local = datetime.now().astimezone()
    if now_local.hour != settings.auto_digest_hour_local:
        return

    today_iso = now_local.strftime("%Y-%m-%d")
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM daily_digest WHERE day = ?",
            (today_iso,),
        )
        if await cursor.fetchone() is not None:
            return

    try:
        text = await summarise_day(now_local)
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
