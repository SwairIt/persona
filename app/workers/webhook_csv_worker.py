"""Nightly CSV-to-webhook pipeline worker (v1.62).

Sibling of :mod:`app.workers.tag_email_digest_worker` — same shape, a
different fan-out. Once an hour, the loop walks
``webhook_csv_destination`` (migration ``147``) and POSTs a fresh CSV
dump to every row whose ``hour_local`` matches the current local
wall-clock and whose ``last_sent_at`` is at least 23 hours old. The
23-hour floor is the small-step generalisation of the "fire once per
day" contract; six minutes of slack accounts for DST, NTP jitter, and
a worker restart at the firing hour.

Why a hand-rolled hour-cycle loop, not :class:`ClockScheduler`
--------------------------------------------------------------

The shared :class:`app.workers._bases.ClockScheduler` fires a *single*
job at *one* configured hour and marks it done for the day via a kv
marker. This pipeline needs N independent jobs (one per destination
row), each with its *own* hour and its *own* idempotency floor on a
per-row column. ``ClockScheduler`` would force every destination to
share a single hour and a single marker — wrong shape.

Instead this worker uses the same poll-then-fan-out shape as the
per-tag email digest worker: an hour-resolution poll, a per-tick
read of the global enable toggle, and a SQL ``SELECT`` that picks
the rows that are due *right now*. The ClockScheduler abstraction is
referenced here so the next reader knows it was considered and
rejected, not overlooked.

Toggles
-------

* ``webhook_csv_pipeline_enabled`` (kv_settings) — ``"1"`` to fire,
  anything else off. Default ``"0"`` so a fresh install is silent
  until the operator opts in.

Failure handling
----------------

:func:`app.webhook_csv_pipeline.send_destination` catches per-row
failures itself and writes the outcome into the row's
``last_status_code`` / ``last_error`` columns. The only failure modes
that reach this loop are programming errors (bad SQL, schema drift)
and they are logged at ``exception`` level without killing the loop.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.webhook_csv_pipeline import send_destination
from app.workers.heartbeat import beat

log = get_logger("persona.workers.webhook_csv")

# kv row name shared with :mod:`app.web.routes.webhook_csv_pipeline`.
_KV_ENABLED: str = "webhook_csv_pipeline_enabled"

# Hour-cycle poll. A destination can choose any hour 0..23, so the
# loop must wake every hour to give every hour-slot a chance. Matches
# the cadence of :mod:`app.workers.tag_email_digest_worker`.
_POLL_INTERVAL_SECONDS: int = 3600

# Minimum age of ``last_sent_at`` before a row is eligible again.
# 23 hours, not 24, so a destination configured for 05:00 still fires
# the next 05:00 even if the previous send slipped by a few minutes
# (DST, NTP nudge, worker restart at the firing hour).
_RESEND_FLOOR_HOURS: int = 23

# Worker slug used by heartbeat + log lines. Kept short so the
# heartbeat dashboard column does not truncate.
_WORKER_NAME: str = "webhook-csv"


async def _enabled() -> bool:
    """Return ``True`` when the top-level toggle is the literal ``"1"``.

    Default off. The kv row is consulted on every tick so toggling at
    runtime takes effect immediately — no restart, no SIGHUP.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return False
    return raw.strip() == "1"


async def _list_due(now: datetime) -> list[int]:
    """Return ids of destinations due to fire on this hour-tick.

    Three clauses, all parametrised:

    * ``enabled = 1`` — paused rows are ignored even if their hour
      matches; the operator can pause a destination without deleting
      its custom headers / URL.
    * ``hour_local = ?`` — the wall-clock hour the operator picked.
    * ``last_sent_at IS NULL OR last_sent_at < ?`` — the 23-hour
      floor. Comparing ISO-8601 strings lexicographically is valid
      because the floor is itself a full ISO-8601 string and every
      stored value follows the same canonical format.

    Returns just the ids; the per-row CSV build + POST happens inside
    :func:`send_destination` and re-reads the row to avoid carrying a
    snapshot that might race with a settings-page edit.
    """
    floor = (now - timedelta(hours=_RESEND_FLOOR_HOURS)).isoformat()
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM webhook_csv_destination "
            "WHERE enabled = 1 "
            "  AND hour_local = ? "
            "  AND (last_sent_at IS NULL OR last_sent_at < ?) "
            "ORDER BY id ASC",
            (int(now.hour), floor),
        )
        rows = await cursor.fetchall()
    return [int(row["id"]) for row in rows]


async def run_webhook_csv_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan entry point — hourly poll over ``webhook_csv_destination``.

    The loop:

    * heartbeats every tick so the worker shows up in the dashboard,
    * skips the body when the kv toggle is off,
    * computes the current local datetime *once* per tick and passes
      it into both :func:`_list_due` and :func:`send_destination` so
      the SQL filter and the date-window in the CSV body see the
      same wall-clock,
    * dispatches each due id sequentially. A CSV dump + POST is fast
      compared with the hour-cadence; running them in parallel would
      pile network spikes on a single hour boundary for marginal
      latency wins.
    * logs the per-tick counter at info when any work happened,
    * waits :data:`_POLL_INTERVAL_SECONDS` (or until ``stop_event``
      fires) before the next iteration.
    """
    stop = stop_event or asyncio.Event()
    log.info(
        "worker.started",
        worker=_WORKER_NAME,
        poll_seconds=_POLL_INTERVAL_SECONDS,
    )

    while not stop.is_set():
        await beat(_WORKER_NAME)
        try:
            if not await _enabled():
                log.debug("webhook_csv.disabled")
            else:
                now = datetime.now().astimezone()
                due_ids = await _list_due(now)
                considered = 0
                sent = 0
                errors = 0
                for dest_id in due_ids:
                    considered += 1
                    try:
                        result = await send_destination(
                            dest_id, now_iso=now.isoformat()
                        )
                    except Exception as exc:
                        log.exception(
                            "webhook_csv.send.crashed",
                            worker=_WORKER_NAME,
                            dest_id=dest_id,
                            error=str(exc),
                        )
                        errors += 1
                        continue
                    if result.get("status") == "sent":
                        sent += 1
                    elif result.get("status") in {
                        "http_error",
                        "transport_error",
                    }:
                        errors += 1
                if considered or errors:
                    log.info(
                        "webhook_csv.cycle",
                        worker=_WORKER_NAME,
                        considered=considered,
                        sent=sent,
                        errors=errors,
                    )
        except asyncio.CancelledError:
            log.info("worker.cancelled", worker=_WORKER_NAME)
            raise
        except Exception as exc:
            log.exception(
                "worker.iteration_failed",
                worker=_WORKER_NAME,
                error=str(exc),
            )

        try:
            await asyncio.wait_for(
                stop.wait(), timeout=_POLL_INTERVAL_SECONDS
            )
        except TimeoutError:
            continue

    log.info("worker.stopped", worker=_WORKER_NAME)


__all__ = ["run_webhook_csv_worker"]
