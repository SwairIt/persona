"""Smart-pin suggester scheduler (v1.50).

Fires once a day at the operator's chosen local hour and calls
:func:`app.llm.smart_pin.suggest_smart_pins` for **yesterday**. The
LLM picks 1-3 of yesterday's screenshots that look IMPORTANT (code
review approvals, decisions, key events) and the picks land in
``smart_pin_suggestion`` for user review on ``/memory/smart-pins``.

Why yesterday and not today: the day must be *complete* so the model
sees the full context (a "code review approval" at 09:00 might be
followed by a revert at 17:00 that changes the picture). Yesterday's
shots are also already through OCR and embeddings, so the prompt has
clean OCR excerpts to chew on.

Configuration lives in ``kv_settings`` so the operator can change it
without restarting the daemon (same pattern as the auto-pin engine):

* ``smart_pin_hour_local`` (default ``8``) — local-time hour at which
  to fire. ``8`` means "first thing in the morning, after I open my
  laptop".
* ``smart_pin_enabled`` (default ``"0"``) — feature gate. Off by
  default because it consumes BYO LLM credits; the operator must opt
  in from the settings UI (or set the kv row manually).
* ``smart_pin_last_fired`` — idempotency marker maintained by
  :class:`app.workers._bases.ClockScheduler`. Stores the last
  ``YYYY-MM-DD`` we successfully ran on so a daemon restart inside
  the trigger hour does not double-fire.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from app.llm.smart_pin import suggest_smart_pins
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers._bases import ClockScheduler

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.workers.smart_pin")

#: 30-minute poll cadence matches every other ClockScheduler in the
#: codebase. The scheduler only fires once per local-day, so a coarser
#: tick than 30 min would risk missing the configured hour.
POLL_INTERVAL_SECONDS: int = 1800

#: kv_settings rows the scheduler reads on every tick.
_KV_HOUR: str = "smart_pin_hour_local"
_KV_ENABLED: str = "smart_pin_enabled"
_KV_MARKER: str = "smart_pin_last_fired"

#: Defaults when the operator has not written anything to kv yet.
_DEFAULT_HOUR: int = 8
_DEFAULT_ENABLED: bool = False


async def _hour_local_getter() -> int:
    """Read the trigger hour from kv, falling back to :data:`_DEFAULT_HOUR`."""
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_HOUR)
    if raw is None:
        return _DEFAULT_HOUR
    try:
        hour = int(str(raw).strip())
    except ValueError:
        return _DEFAULT_HOUR
    if hour < 0 or hour > 23:
        return _DEFAULT_HOUR
    return hour


async def _enabled_getter() -> bool:
    """Read the feature flag from kv. ``"1"`` / ``"true"`` enable; default off.

    Matches the parsing convention used by the auto-pin engine's
    ``auto_pin_enabled`` row, so the same settings-page toggle can
    write either feature without surprise.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return _DEFAULT_ENABLED
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


async def _job_suggest() -> None:
    """One scheduler invocation — suggest pins for yesterday's date."""
    yesterday = (datetime.now(tz=UTC).astimezone().date() - timedelta(days=1))
    day_iso = yesterday.isoformat()
    log.info("smart_pin_worker.fire", day=day_iso)
    result = await suggest_smart_pins(day_iso)
    log.info(
        "smart_pin_worker.done",
        day=day_iso,
        status=result["status"],
        candidates=result["candidates"],
        suggested=result["suggested"],
    )


async def run_smart_pin_worker(stop_event: asyncio.Event | None = None) -> None:
    """Lifespan entry point — registers a :class:`ClockScheduler`."""
    scheduler = ClockScheduler(
        name="smart-pin-suggester",
        hour_local_getter=_hour_local_getter,
        enabled_getter=_enabled_getter,
        marker_kv=_KV_MARKER,
        job=_job_suggest,
        poll_seconds=POLL_INTERVAL_SECONDS,
    )
    await scheduler.run(stop_event)


__all__ = [
    "POLL_INTERVAL_SECONDS",
    "run_smart_pin_worker",
]
