"""Daily stale-note pruner scheduler (v1.49).

Wakes every 30 minutes and, when the local-clock hour matches
``stale_note_pruner_hour_local`` (default 3 — 03:00, deep night), calls
:func:`app.stale_note_pruner.prune_stale` with ``dry_run=False`` to
soft-delete inbox notes whose body is empty or whitespace-only and
older than the configured cutoff.

Wraps :class:`app.workers._bases.ClockScheduler` so the daily-once
guarantee (one fire per calendar day, idempotent across restarts) is
the same as the audit-log rotator and the digest schedulers. The
per-day marker lives in ``kv_settings`` under
``stale_note_pruner_last_fired``.

Toggles (all kv rows; operator edits from the admin page)
---------------------------------------------------------
* ``stale_note_pruner_enabled``    — ``"1"`` = on, anything else = off.
  Default ``"0"`` (OFF). Unlike the audit-log rotator (which is
  destructive but write-then-delete), this one stamps
  ``deleted_at`` on rows the *user* created — even though it's a
  soft-delete, we want explicit operator opt-in via the admin page.
* ``stale_note_pruner_hour_local`` — integer 0..23. Default ``3``.
  3 AM is far enough from "human awake hours" that the brief UPDATE
  doesn't compete with anything else.

The age cutoff itself (``min_age_days``) is hard-coded to the
:data:`app.stale_note_pruner.DEFAULT_MIN_AGE_DAYS` constant in this
v1.49 cut — adding a kv-tunable cutoff is a future enhancement once we
have operational data on what the right window actually is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.logging_setup import get_logger
from app.stale_note_pruner import prune_stale
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers._bases import ClockScheduler

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.workers.stale_note_pruner")

_KV_HOUR: str = "stale_note_pruner_hour_local"
_KV_ENABLED: str = "stale_note_pruner_enabled"
_MARKER_KV: str = "stale_note_pruner_last_fired"

_DEFAULT_HOUR: int = 3
_POLL_INTERVAL_SECONDS: int = 1800  # 30 min — same cadence as siblings


async def _hour_getter() -> int:
    """Read the configured local-time hour; fall back to ``3``.

    Malformed values (non-int, out of 0..23) collapse to the default so
    a fat-finger in the settings UI cannot park the scheduler.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_HOUR)
    if raw is None:
        return _DEFAULT_HOUR
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        log.warning("stale_note_pruner.hour.invalid", raw=raw)
        return _DEFAULT_HOUR
    if 0 <= value <= 23:
        return value
    log.warning("stale_note_pruner.hour.out_of_range", value=value)
    return _DEFAULT_HOUR


async def _enabled_getter() -> bool:
    """Return whether the pruner is currently enabled. Default ``False``.

    The pruner is destructive (soft-delete is reversible, but still a
    user-visible mutation), so we default OFF and require the operator
    to flip the kv row to ``"1"`` via the admin page after they've
    previewed at least one run.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return False
    return raw.strip() == "1"


async def _job_prune() -> None:
    """One job invocation — call the pruner with ``dry_run=False`` and log.

    A ``count`` of zero is the happy no-op path; we still want the line
    in the log so the operator can confirm the worker fired today even
    when there was nothing to do. Any unhandled exception inside
    ``prune_stale`` bubbles up to :class:`ClockScheduler`, which treats
    it as "retry on the next 30-min tick" (the marker is only stamped
    on success).
    """
    log.info("stale_note_pruner.job.start")
    result = await prune_stale(dry_run=False)
    log.info(
        "stale_note_pruner.job.done",
        count=result.get("count", 0),
        age_threshold_days=result.get("age_threshold_days", 0),
    )


async def run_stale_note_pruner_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan entry point — drives a :class:`ClockScheduler`."""
    scheduler = ClockScheduler(
        name="stale-note-pruner",
        hour_local_getter=_hour_getter,
        enabled_getter=_enabled_getter,
        marker_kv=_MARKER_KV,
        job=_job_prune,
        poll_seconds=_POLL_INTERVAL_SECONDS,
    )
    await scheduler.run(stop_event)


__all__ = ["run_stale_note_pruner_worker"]
