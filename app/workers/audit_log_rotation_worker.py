"""Daily audit-log rotation scheduler (v1.48).

Wakes every 30 minutes, and when the local-clock hour matches
``audit_log_rotation_hour_local`` (default 4 — 04:00, deep in the
quiet hours) it calls :func:`app.audit_log_rotation.rotate_audit_log`.
That function is a no-op when the table has not exceeded
``audit_log_rotation_keep_rows`` (default 5000), so most days the
worker logs ``not_needed`` and goes back to sleep.

Wraps :class:`app.workers._bases.ClockScheduler` so the daily-once
guarantee (one fire per calendar day, idempotent across restarts) is
the same as the digest scheduler and the daily-email scheduler. The
per-day marker lives in ``kv_settings`` under
``audit_log_rotation_last_fired``.

Toggles (all kv rows; operator can edit from the settings page)
---------------------------------------------------------------
* ``audit_log_rotation_enabled``     — ``"1"`` = on, anything else = off.
  Default ``"1"``. Unusually for Persona, this defaults ON: rotation
  never destroys data (rows are written to disk before deletion) and
  the table grows unbounded otherwise. A user who actively wants the
  full append-only history can flip it off; everyone else just wants
  it to keep itself trim.
* ``audit_log_rotation_hour_local``  — integer 0..23. Default ``4``.
  4 AM is far enough from "human awake hours" that the brief I/O burst
  doesn't compete with anything else.
* ``audit_log_rotation_keep_rows``   — integer ≥ 0. Default ``5000``.
  Empirically ~3 months of rows on a single-user install.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.audit_log_rotation import rotate_audit_log
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers._bases import ClockScheduler

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.workers.audit_log_rotation")

_KV_HOUR: str = "audit_log_rotation_hour_local"
_KV_ENABLED: str = "audit_log_rotation_enabled"
_KV_KEEP_ROWS: str = "audit_log_rotation_keep_rows"
_MARKER_KV: str = "audit_log_rotation_last_fired"

_DEFAULT_HOUR: int = 4
_DEFAULT_KEEP_ROWS: int = 5000
_POLL_INTERVAL_SECONDS: int = 1800  # 30 min — same cadence as siblings


async def _hour_getter() -> int:
    """Read the configured local-time hour; fall back to ``4``.

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
        log.warning("audit_log_rotation.hour.invalid", raw=raw)
        return _DEFAULT_HOUR
    if 0 <= value <= 23:
        return value
    log.warning("audit_log_rotation.hour.out_of_range", value=value)
    return _DEFAULT_HOUR


async def _enabled_getter() -> bool:
    """Return whether rotation is currently enabled. Default ``True``.

    Unusually for Persona toggles we default to on: the rotator never
    destroys data (rows are written to disk before deletion) and the
    table grows unbounded otherwise. A user who actively wants the
    full append-only history can flip the kv row to ``"0"``.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return True
    return raw.strip() == "1"


async def _keep_rows_getter() -> int:
    """Read the configured keep-rows budget; fall back to ``5000``.

    Negative / non-integer values collapse to the default.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_KEEP_ROWS)
    if raw is None:
        return _DEFAULT_KEEP_ROWS
    try:
        value = int(raw.strip())
    except (ValueError, AttributeError):
        log.warning("audit_log_rotation.keep_rows.invalid", raw=raw)
        return _DEFAULT_KEEP_ROWS
    if value < 0:
        log.warning("audit_log_rotation.keep_rows.negative", value=value)
        return _DEFAULT_KEEP_ROWS
    return value


async def _job_rotate() -> None:
    """One job invocation — call the rotator and log the outcome.

    We deliberately do NOT raise on ``not_needed`` (that's the happy
    no-op path) and we do NOT raise on ``ok`` either — both should
    advance the per-day marker. Only an unhandled exception inside
    ``rotate_audit_log`` would skip the marker; ``ClockScheduler``
    treats that as "retry on the next 30-min tick".
    """
    keep_rows = await _keep_rows_getter()
    log.info("audit_log_rotation.job.start", keep_rows=keep_rows)
    result = await rotate_audit_log(keep_rows=keep_rows)
    log.info(
        "audit_log_rotation.job.done",
        status=result.get("status"),
        rows_archived=result.get("rows_archived", 0),
        file_size_bytes=result.get("file_size_bytes", 0),
    )


async def run_audit_log_rotation_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan entry point — drives a :class:`ClockScheduler`."""
    scheduler = ClockScheduler(
        name="audit-log-rotation-worker",
        hour_local_getter=_hour_getter,
        enabled_getter=_enabled_getter,
        marker_kv=_MARKER_KV,
        job=_job_rotate,
        poll_seconds=_POLL_INTERVAL_SECONDS,
    )
    await scheduler.run(stop_event)


__all__ = ["run_audit_log_rotation_worker"]
