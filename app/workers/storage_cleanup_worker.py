"""Background worker that runs :func:`run_cleanup` nightly.

The worker polls every hour and only actually deletes when 24 h have
passed since the last successful run. This avoids hammering the disk
when the server gets restarted often (each restart would otherwise
trigger a cleanup pass).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from app.logging_setup import get_logger
from app.storage_management import list_cleanup_runs, run_cleanup

log = get_logger("persona.workers.storage_cleanup")

# Poll cadence — cheap (one SQL read of the log table) so an hourly
# heartbeat keeps the loop responsive without burdening the DB.
_POLL_INTERVAL_SECONDS = 60 * 60  # 1 hour

# How recently a cleanup must have completed before we skip this tick.
_MIN_INTERVAL_BETWEEN_RUNS = timedelta(hours=24)


async def _should_run_now() -> bool:
    """True iff no successful cleanup has finished in the last 24 h."""
    runs = await list_cleanup_runs(limit=5)
    for r in runs:
        if r["error"] is not None or r["finished_at"] is None:
            continue
        # Worker-initiated runs only — manual runs from the dashboard
        # don't count as "we already swept", because the user might
        # want the nightly worker to keep the schedule honest.
        if r["trigger_source"] != "worker":
            continue
        try:
            finished = datetime.fromisoformat(
                str(r["finished_at"]).replace(" ", "T")
            )
        except ValueError:
            continue
        # SQLite returns naive UTC strings. Tag them so the comparison
        # against ``datetime.now(UTC)`` doesn't blow up.
        if finished.tzinfo is None:
            finished = finished.replace(tzinfo=UTC)
        if datetime.now(UTC) - finished < _MIN_INTERVAL_BETWEEN_RUNS:
            return False
        # First worker-run we found is the most recent (rows sorted DESC).
        return True
    # No prior worker run on record — run now.
    return True


async def run() -> None:
    """The worker loop. Spawned from the app lifespan startup."""
    log.info(
        "storage_cleanup_worker.start", poll_seconds=_POLL_INTERVAL_SECONDS
    )
    while True:
        try:
            if await _should_run_now():
                await run_cleanup(trigger_source="worker")
        except asyncio.CancelledError:
            log.info("storage_cleanup_worker.cancelled")
            raise
        except Exception:
            log.exception("storage_cleanup_worker.tick_failed")
        try:
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            log.info("storage_cleanup_worker.cancelled")
            raise
