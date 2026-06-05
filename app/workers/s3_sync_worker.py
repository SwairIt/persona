"""Nightly S3/R2 cross-machine sync — ClockScheduler entry point.

This is a "fire once a day at hour-local" worker — the destination is
remote and the payload (encrypted SQLite + thumbnail blobs) is sized in
hundreds of MB on busy installs, so we never want to fire it more than
once per local calendar day.

State lives in three kv rows:

* ``s3_sync_enabled`` — gate. ``"1"`` enables the worker; default is
  off so a fresh install never touches the network.
* ``s3_sync_hour_local`` — local hour (0..23) at which the run should
  fire. Default ``3`` (3 AM local) — outside busy capture windows on
  most installs.
* ``s3_sync_last_fired`` — managed by :class:`ClockScheduler`; holds
  the ``YYYY-MM-DD`` of the most recent successful run. The scheduler
  refuses to fire again on the same date.

The job raises a :class:`RuntimeError` whenever
:func:`app.s3_sync.sync_to_s3` reports a non-``ok`` status. Raising is
load-bearing: it tells the :class:`ClockScheduler` to skip the
marker-advance step, so the next tick (30 minutes later) will retry
during the same target hour. After the hour rolls over, the scheduler
simply parks until tomorrow.

The lifespan task list in :mod:`app.web.main` is NOT edited from this
patch — wiring is expected to happen in a later release that also adds
the ``[s3]`` extra to :file:`pyproject.toml`. The worker is fully
exercised manually via the ``/api/s3-sync/run-now`` button until then.
"""

from __future__ import annotations

from typing import Final

from app.logging_setup import get_logger
from app.s3_sync import sync_to_s3
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers._bases import ClockScheduler

log = get_logger("persona.workers.s3_sync")

_KV_ENABLED: Final[str] = "s3_sync_enabled"
_KV_HOUR_LOCAL: Final[str] = "s3_sync_hour_local"
_KV_LAST_FIRED: Final[str] = "s3_sync_last_fired"

_DEFAULT_HOUR_LOCAL: Final[int] = 3
"""3 AM local — quiet hours on most setups, outside the typical
capture window so the long-running upload doesn't compete with
real-time screenshot pipelines."""

POLL_INTERVAL_SECONDS: Final[int] = 1800
"""30 minutes — matches every other ClockScheduler in this codebase.
Tight enough to catch the configured hour at least once, loose enough
to keep heartbeats cheap."""

_WORKER_NAME: Final[str] = "s3-sync-worker"


# ---------------------------------------------------------------------------
# kv getters — wrapped in async helpers so ClockScheduler can call them
# without knowing where the values came from. Each getter is defensive:
# a corrupt row falls back to the documented default rather than
# crashing the lifespan.
# ---------------------------------------------------------------------------


async def _hour_local_getter() -> int:
    """Read ``s3_sync_hour_local`` from kv. Out-of-range or invalid → default."""
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_HOUR_LOCAL)
    if not raw:
        return _DEFAULT_HOUR_LOCAL
    try:
        value = int(raw.strip())
    except ValueError:
        return _DEFAULT_HOUR_LOCAL
    if value < 0 or value > 23:
        return _DEFAULT_HOUR_LOCAL
    return value


async def _enabled_getter() -> bool:
    """Read ``s3_sync_enabled`` from kv. Default OFF — see module docstring."""
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    return (raw or "").strip() == "1"


# ---------------------------------------------------------------------------
# The job — invoked at most once per local day by ClockScheduler.
# ---------------------------------------------------------------------------


async def _job_sync() -> None:
    """Run one full S3 sync. Raise on any non-``ok`` status.

    The raise contract is critical: ``ClockScheduler`` only advances
    the ``s3_sync_last_fired`` marker when the job returns normally.
    Any non-``ok`` status (missing config, missing deps, upload
    failure) leaves the marker untouched, which means the next 30-min
    poll inside the same target hour gets a chance to retry once the
    user finishes configuration.
    """
    result = await sync_to_s3()
    status = result.get("status", "unknown")

    if status == "ok":
        log.info(
            "s3_sync.worker.fired",
            db_uploaded=result.get("db_uploaded", 0),
            thumbnails_uploaded=result.get("thumbnails_uploaded", 0),
            bytes_total=result.get("bytes_total", 0),
        )
        return

    log.warning(
        "s3_sync.worker.skipped",
        status=status,
        error=result.get("error"),
    )
    msg = f"s3_sync skipped: status={status}"
    raise RuntimeError(msg)


# ---------------------------------------------------------------------------
# Lifespan entry point
# ---------------------------------------------------------------------------


async def run_s3_sync_worker() -> None:
    """Long-running coroutine — block on the scheduler's stop event.

    Not currently registered in :mod:`app.web.main`'s lifespan list — a
    follow-up release will add it together with the ``[s3]`` optional
    extra. The worker is still safe to start manually for tests; it
    no-ops when the kv config is missing.
    """
    scheduler = ClockScheduler(
        name=_WORKER_NAME,
        hour_local_getter=_hour_local_getter,
        enabled_getter=_enabled_getter,
        marker_kv=_KV_LAST_FIRED,
        job=_job_sync,
        poll_seconds=POLL_INTERVAL_SECONDS,
    )
    await scheduler.run()


__all__ = [
    "POLL_INTERVAL_SECONDS",
    "run_s3_sync_worker",
]
