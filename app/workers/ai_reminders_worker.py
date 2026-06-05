"""AI-suggested daily reminders scheduler (v1.46).

Wakes up periodically; when the local-clock hour matches
``ai_reminders_hour_local`` (default ``22`` — 22:00) it invokes
:func:`app.llm.ai_reminders.generate_reminders` for *today* so the user
wakes up to a freshly-suggested list of "do not forget" items for
tomorrow.

Wraps :class:`app.workers._bases.ClockScheduler` so the daily-once
guarantee (one fire per calendar day, idempotent across restarts) is
the same as the digest scheduler, the daily-email scheduler, and the
day-end summary scheduler. The marker row in ``kv_settings`` is
``ai_reminders_last_fired``.

Toggles
-------
* ``ai_reminders_enabled`` (kv) — ``"1"`` = on, anything else = off.
  Defaults to ``0`` so the feature is opt-in.
* ``ai_reminders_hour_local`` (kv) — integer 0..23. Defaults to ``22``.

The LLM call is best-effort: a missing BYO key returns
``status="missing_config"``, a network blip returns
``status="llm_failed"``. The scheduler logs and continues — neither
prevents the marker from advancing, because the next day's tick will
have its own fresh signal to work from anyway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.llm.ai_reminders import generate_reminders
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers._bases import ClockScheduler

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.workers.ai_reminders")

_KV_HOUR: str = "ai_reminders_hour_local"
_KV_ENABLED: str = "ai_reminders_enabled"
_MARKER_KV: str = "ai_reminders_last_fired"

_DEFAULT_HOUR: int = 22
_POLL_INTERVAL_SECONDS: int = 1800  # 30 min — same cadence as daily-email


async def _hour_getter() -> int:
    """Read the configured local-time hour; fall back to ``22``.

    A malformed value (non-int, out of 0..23) collapses to the default
    so a fat-finger in the settings UI can't park the scheduler
    permanently.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_HOUR)
    if raw is None:
        return _DEFAULT_HOUR
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        log.warning("ai_reminders.hour.invalid", raw=raw)
        return _DEFAULT_HOUR
    if 0 <= value <= 23:
        return value
    log.warning("ai_reminders.hour.out_of_range", value=value)
    return _DEFAULT_HOUR


async def _enabled_getter() -> bool:
    """Return whether the scheduler should fire. Defaults to disabled.

    Following the existing project pattern (see ``app_budget_worker``)
    we accept ``"1"`` as on and treat anything else — empty, ``"0"``,
    typo — as off. That keeps the toggle robust against operator
    mistakes in the settings UI.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return False
    return raw.strip() == "1"


async def _job_generate_for_today() -> None:
    """Generate tomorrow-facing reminders from today's signal.

    The LLM is asked to look back at *today* (the day we are still in)
    and surface what the user should not forget tomorrow. We log the
    return shape but never raise: ``ClockScheduler`` would otherwise
    skip the per-day marker and we'd re-fire on the next 30-min tick.
    """
    from datetime import datetime  # noqa: PLC0415 — keep import scope local

    today_iso = datetime.now().astimezone().date().isoformat()
    log.info("ai_reminders.job.start", day=today_iso)
    result = await generate_reminders(today_iso)
    log.info(
        "ai_reminders.job.done",
        day=today_iso,
        status=result["status"],
        count=result["count"],
    )


async def run_ai_reminders_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan entry point — drives a :class:`ClockScheduler`."""
    scheduler = ClockScheduler(
        name="ai-reminders-worker",
        hour_local_getter=_hour_getter,
        enabled_getter=_enabled_getter,
        marker_kv=_MARKER_KV,
        job=_job_generate_for_today,
        poll_seconds=_POLL_INTERVAL_SECONDS,
    )
    await scheduler.run(stop_event)


__all__ = ["run_ai_reminders_worker"]
