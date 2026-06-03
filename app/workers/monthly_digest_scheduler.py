"""Once-a-month worker that generates and stores the monthly LLM summary.

Polls every hour. When local time is the 1st of any calendar month at
``monthly_digest_hour_local`` and we have not yet generated a digest for the
PREVIOUS calendar month (the one that just closed), call
:func:`app.llm.monthly_summariser.summarise_month` and persist the result
into ``monthly_digest``. Silent skip when BYO LLM is not configured.

The job is idempotent: every store goes through ``INSERT OR REPLACE`` keyed on
the ``YYYY-MM`` month string, and the pre-flight check short-circuits when a
row already exists, so multiple poll iterations within the trigger hour cannot
double-bill the LLM provider.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.llm import LLMNotConfigured
from app.llm.monthly_summariser import summarise_month
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

log = get_logger("persona.monthly_digest")

POLL_INTERVAL_SECONDS = 3600.0  # 1 hour


def _previous_month_iso(today: datetime) -> str:
    """Return ``YYYY-MM`` for the calendar month before ``today``."""
    year = today.year
    month = today.month - 1
    if month == 0:
        month = 12
        year -= 1
    return f"{year:04d}-{month:02d}"


async def run_monthly_digest_scheduler(
    controller: CaptureController | None = None,
) -> None:
    ctrl = controller or get_controller()
    settings = get_settings()

    if not settings.monthly_digest_enabled:
        log.info("monthly_digest_scheduler.disabled")
        await ctrl.stop_event.wait()
        return

    log.info(
        "monthly_digest_scheduler.started",
        hour=settings.monthly_digest_hour_local,
    )

    while not ctrl.stop_event.is_set():
        await beat("monthly-digest-scheduler")
        try:
            await _maybe_generate()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("monthly_digest_scheduler.failed", error=str(exc))

        try:
            await asyncio.wait_for(
                ctrl.stop_event.wait(),
                timeout=POLL_INTERVAL_SECONDS,
            )
        except TimeoutError:
            continue


async def _maybe_generate() -> None:
    settings = get_settings()
    now_local = datetime.now().astimezone()

    # Trigger only on the 1st of the month at the configured local hour.
    if now_local.day != 1:
        return
    if now_local.hour != settings.monthly_digest_hour_local:
        return

    month_iso = _previous_month_iso(now_local)

    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM monthly_digest WHERE month = ?",
            (month_iso,),
        )
        if await cursor.fetchone() is not None:
            return

    try:
        text = await summarise_month(month_iso)
    except LLMNotConfigured as exc:
        log.warning("monthly_digest_scheduler.no_llm", error=str(exc))
        return

    async with get_connection() as conn:
        await conn.execute(
            "INSERT OR REPLACE INTO monthly_digest "
            "(month, body, provider) VALUES (?, ?, ?)",
            (month_iso, text, settings.byo_api_provider or None),
        )
        await conn.commit()

    log.info(
        "monthly_digest_scheduler.generated",
        month=month_iso,
        chars=len(text),
    )
