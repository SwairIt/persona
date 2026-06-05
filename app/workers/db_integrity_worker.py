"""Daily DB integrity quick-check + ANALYZE scheduler (v1.51).

Wakes every 30 minutes, and when the local-clock hour matches
``db_integrity_check_hour_local`` (default 4 — 04:00, deep in the
quiet hours and the same slot the audit-log rotator runs in) it calls
:func:`app.db_integrity.run_quick_check` followed by
:func:`app.db_integrity.run_analyze`.

We deliberately do NOT fire ``run_full_check`` from the scheduler:
``PRAGMA integrity_check`` rewalks every page + index and on a
multi-GB single-user install can run for tens of seconds. The cheap
``quick_check`` catches the same torn-page / free-list damage in the
common case; the operator opts into a slow full check via the "Run
Full Check" button on the admin page when they actually need it.

Wraps :class:`app.workers._bases.ClockScheduler` so the daily-once
guarantee (one fire per calendar day, idempotent across restarts) is
the same as every other nightly scheduler in this directory. The
per-day marker lives in ``kv_settings`` under
``db_integrity_check_last_fired``.

Toggles (all kv rows; operator can edit from the settings page)
---------------------------------------------------------------
* ``db_integrity_check_enabled``     — ``"1"`` = on, anything else =
  off. Default ``"1"``. Like the audit-log rotator we default ON: the
  check is read-only on the data, the ANALYZE pass is short, and a
  silent slow build-up of free-list damage is exactly what this is
  meant to surface.
* ``db_integrity_check_hour_local``  — integer 0..23. Default ``4``.
  4 AM is far enough from "human awake hours" that the brief I/O
  burst doesn't compete with anything else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.db_integrity import run_analyze, run_quick_check
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers._bases import ClockScheduler

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.workers.db_integrity")

_KV_HOUR: str = "db_integrity_check_hour_local"
_KV_ENABLED: str = "db_integrity_check_enabled"
_MARKER_KV: str = "db_integrity_check_last_fired"

_DEFAULT_HOUR: int = 4
_POLL_INTERVAL_SECONDS: int = 1800  # 30 min — same cadence as siblings


async def _hour_getter() -> int:
    """Read the configured local-time hour; fall back to ``4``.

    Malformed values (non-int, out of 0..23) collapse to the default
    so a fat-finger in the settings UI cannot park the scheduler.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_HOUR)
    if raw is None:
        return _DEFAULT_HOUR
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        log.warning("db_integrity.hour.invalid", raw=raw)
        return _DEFAULT_HOUR
    if 0 <= value <= 23:
        return value
    log.warning("db_integrity.hour.out_of_range", value=value)
    return _DEFAULT_HOUR


async def _enabled_getter() -> bool:
    """Return whether the nightly check is currently enabled. Default ``True``.

    Like the audit-log rotator we default to on: the PRAGMAs are
    read-only on the data, ANALYZE is short, and a silent slow
    build-up of free-list damage is exactly what this is meant to
    surface. The operator flips the kv row to ``"0"`` to silence it.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return True
    return raw.strip() == "1"


async def _job_run_nightly() -> None:
    """One job invocation — quick_check + analyze, log the outcomes.

    We run both regardless of the quick_check verdict — ANALYZE is
    short and benefits even when the integrity check turned up a
    warning. Exceptions from inside either call would skip the per-day
    marker; :class:`ClockScheduler` treats that as "retry on the next
    30-min tick".
    """
    log.info("db_integrity.job.start")
    quick = await run_quick_check()
    log.info(
        "db_integrity.job.quick_done",
        status=quick.get("status"),
        duration_ms=quick.get("duration_ms", 0),
        db_size_bytes=quick.get("db_size_bytes", 0),
    )
    analyze = await run_analyze()
    log.info(
        "db_integrity.job.analyze_done",
        status=analyze.get("status"),
        duration_ms=analyze.get("duration_ms", 0),
        db_size_bytes=analyze.get("db_size_bytes", 0),
    )


async def run_db_integrity_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan entry point — drives a :class:`ClockScheduler`."""
    scheduler = ClockScheduler(
        name="db-integrity-check",
        hour_local_getter=_hour_getter,
        enabled_getter=_enabled_getter,
        marker_kv=_MARKER_KV,
        job=_job_run_nightly,
        poll_seconds=_POLL_INTERVAL_SECONDS,
    )
    await scheduler.run(stop_event)


__all__ = ["run_db_integrity_worker"]
