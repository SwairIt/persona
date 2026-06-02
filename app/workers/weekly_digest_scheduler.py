"""Once-a-week worker that generates and stores the weekly LLM summary.

Polls every 30 minutes. When local time is Monday at
`weekly_digest_hour_local` and we have not yet generated a digest for the
PREVIOUS calendar week (the just-completed Mon→Sun), call
`summarise_week` and persist into `weekly_digest`. Silent skip when BYO
LLM is not configured.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from app.llm import LLMNotConfigured
from app.llm.weekly_summariser import summarise_week
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.workers.control import CaptureController, get_controller

log = get_logger("persona.weekly_digest_scheduler")

POLL_INTERVAL_SECONDS = 1800.0  # 30 minutes


async def run_weekly_digest_scheduler(
    controller: CaptureController | None = None,
) -> None:
    ctrl = controller or get_controller()
    settings = get_settings()

    if not settings.weekly_digest_enabled:
        log.info("weekly_digest_scheduler.disabled")
        await ctrl.stop_event.wait()
        return

    log.info(
        "weekly_digest_scheduler.started",
        hour=settings.weekly_digest_hour_local,
    )

    while not ctrl.stop_event.is_set():
        try:
            await _maybe_generate()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("weekly_digest_scheduler.failed", error=str(exc))

        try:
            await asyncio.wait_for(
                ctrl.stop_event.wait(),
                timeout=POLL_INTERVAL_SECONDS,
            )
        except asyncio.TimeoutError:
            continue


async def _maybe_generate() -> None:
    settings = get_settings()
    now_local = datetime.now().astimezone()

    # Run only on Monday at the configured hour.
    if now_local.weekday() != 0:
        return
    if now_local.hour != settings.weekly_digest_hour_local:
        return

    last_week_monday = (now_local.date() - timedelta(days=7))
    week_start_iso = last_week_monday.isoformat()

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM weekly_digest WHERE week_start = ?",
            (week_start_iso,),
        )
        if await cursor.fetchone() is not None:
            return

    try:
        text = await summarise_week(last_week_monday)
    except LLMNotConfigured as exc:
        log.warning("weekly_digest_scheduler.no_llm", error=str(exc))
        return

    async with get_connection() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO weekly_digest "
            "(week_start, body, provider) VALUES (?, ?, ?)",
            (week_start_iso, text, settings.byo_api_provider or None),
        )
        await conn.commit()

    log.info(
        "weekly_digest_scheduler.generated",
        week_start=week_start_iso,
        chars=len(text),
    )
