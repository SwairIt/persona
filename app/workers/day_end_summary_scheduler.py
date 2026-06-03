"""Day-end auto-summary scheduler — primes the day TL;DR before midnight.

The TL;DR (``app.llm.day_tldr.summarise_day_tldr``) is generated lazily
when a user opens ``/timeline/{day}`` or ``/digest``. That works fine
during the day, but creates a noticeable wait the first time someone
loads the page the next morning — the route fires a fresh LLM call,
shells out to ``make_client``, and only then returns. This worker
removes that latency.

Behaviour
=========

* Polls every 30 minutes (matches ``daily_email_scheduler``).
* When the local-time clock is at or past ``day_end_summary_hour_local:30``
  (default ``23:30``) **and** no ``day_tldr`` row exists for *today*, it
  calls :func:`summarise_day_tldr` for the current local day.
* :func:`summarise_day_tldr` is itself idempotent — it short-circuits on
  the cache hit — so a duplicate tick the next minute is a no-op. We
  still gate on the cache here to avoid the extra DB round-trip.
* All failures are logged and swallowed; the worker stays alive and
  retries on the next tick. The TL;DR is best-effort and must never
  bring the lifespan down.

The feature is gated by ``settings.day_end_summary_enabled`` (default
False, so existing users are unaffected). When disabled the worker
parks on the stop event exactly like the other opt-in schedulers.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.llm.day_tldr import summarise_day_tldr
from app.logging_setup import get_logger
from app.settings import get_settings
from app.storage.db import get_connection
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

log = get_logger("persona.day_end_summary")

POLL_INTERVAL_SECONDS = 1800.0  # 30 minutes
_TRIGGER_MINUTE = 30  # fire at HH:30, matching the half-hour poll cadence


async def run_day_end_summary_scheduler(
    controller: CaptureController | None = None,
) -> None:
    """Long-running loop. Yields on ``controller.stop_event``."""
    ctrl = controller or get_controller()
    settings = get_settings()

    if not settings.day_end_summary_enabled:
        log.info("day_end_summary.disabled")
        await ctrl.stop_event.wait()
        return

    log.info(
        "day_end_summary.started",
        hour=settings.day_end_summary_hour_local,
    )

    while not ctrl.stop_event.is_set():
        await beat("day-end-summary-scheduler")
        try:
            await _maybe_generate()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("day_end_summary.failed", error=str(exc))

        try:
            await asyncio.wait_for(
                ctrl.stop_event.wait(),
                timeout=POLL_INTERVAL_SECONDS,
            )
        except TimeoutError:
            continue


async def _maybe_generate() -> None:
    """One poll iteration — generate today's TL;DR if past 23:30 and missing."""
    settings = get_settings()
    now_local = datetime.now().astimezone()

    trigger_hour = settings.day_end_summary_hour_local
    # Only fire once the local clock has crossed HH:30 of the trigger hour.
    # The 30-min poll guarantees at least one tick in the HH:30-HH:59 window.
    if now_local.hour < trigger_hour:
        return
    if now_local.hour == trigger_hour and now_local.minute < _TRIGGER_MINUTE:
        return

    today_iso = now_local.date().isoformat()

    # Cheap pre-check: skip the LLM client entirely if the cache row exists.
    # ``summarise_day_tldr`` is itself idempotent, but reading kv saves a
    # connection acquire on every tick after the first.
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM day_tldr WHERE day = ?",
            (today_iso,),
        )
        if await cursor.fetchone() is not None:
            log.debug("day_end_summary.cached", day=today_iso)
            return

    log.info("day_end_summary.generate.start", day=today_iso)
    result = await summarise_day_tldr(today_iso)

    status = result["status"]
    if status == "ok":
        log.info(
            "day_end_summary.generate.done",
            day=today_iso,
            cached=result["cached"],
            chars=len(result["tldr"]),
        )
    else:
        # ``empty`` (no captures today) or ``missing_config`` (no BYO LLM).
        # Both are expected operational outcomes — log at info, retry next tick.
        log.info(
            "day_end_summary.generate.skipped",
            day=today_iso,
            status=status,
        )


__all__ = ["run_day_end_summary_scheduler"]
