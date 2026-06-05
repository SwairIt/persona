"""Per-tag weekly email digest worker (v1.61).

Hourly companion to :mod:`app.workers.email_weekly_digest_worker`.
That worker fires *one* global digest at the configured Sunday hour;
this one fans the same SMTP infra out across every row in
``tag_email_subscription`` (migration ``146_tag_email_subscription.sql``)
whose ``(day_of_week, hour_local)`` slot matches the current local
wall-clock.

Why not :class:`ClockScheduler`
-------------------------------

The shared :class:`app.workers._bases.ClockScheduler` is designed for a
*single* "fire at this hour, mark it done for the day" job. Per-tag
subscriptions need a different shape:

* every hour is potentially a firing hour (different subs picked
  different ``hour_local`` values),
* each tick must look at *all* subs, not one job, and decide row by
  row whether each is due,
* idempotency lives on ``tag_email_subscription.last_sent_at``
  (per-row, six-day floor) rather than a single kv marker.

So this module hand-rolls a poll loop with the same shape every other
worker uses (poll → beat → work → ``stop.wait(timeout=...)``) but
delegates the actual "what's due?" + "ship it" logic to
:func:`app.tag_email_digest.send_due_subscriptions`. ClockScheduler
stays the right tool for the global weekly digest; this is the right
tool for the fan-out.

Toggles
-------

* ``tag_email_digest_enabled`` (kv_settings) — ``"1"`` to fire. Default
  ``"0"`` so a fresh install is silent until the operator opts in.

Failure handling
----------------

The fan-out helper catches per-row failures itself. The only failure
modes that reach the worker loop are programming errors (bad SQL,
schema drift) and they're logged at ``exception`` level without
killing the loop — the next tick simply retries.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.tag_email_digest import send_due_subscriptions
from app.workers.heartbeat import beat

log = get_logger("persona.workers.tag_email_digest")

# kv_settings row name. Renaming here means renaming the settings UI
# too (:mod:`app.web.routes.tag_email_digest`).
_KV_ENABLED: str = "tag_email_digest_enabled"

# 1-hour poll: a per-tag sub can choose any hour 0..23, so the worker
# has to wake every hour to check whether some row is now due. Half-
# hour cadence (the standard for ClockScheduler-backed workers) would
# fire the same row twice in some hours; the per-row 6-day floor
# would mostly catch that, but waking every hour is the simpler
# contract and matches the per-row hour granularity exactly.
_POLL_INTERVAL_SECONDS: int = 3600

# Worker slug used by heartbeat + log lines. Kept short so the
# heartbeat dashboard column doesn't truncate.
_WORKER_NAME: str = "tag-email-digest"


async def _enabled() -> bool:
    """Return ``True`` when the top-level toggle is the literal ``"1"``.

    Mirrors every other opt-in worker in the project — accept ``"1"``
    as on, treat anything else (empty, ``"0"``, typo) as off.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return False
    return raw.strip() == "1"


async def run_tag_email_digest_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan entry point — hourly poll over ``tag_email_subscription``.

    The loop:

    * heartbeats every tick so the worker shows up in the dashboard,
    * skips the body when the kv toggle is off (the row is consulted
      each tick so toggling at runtime takes effect immediately),
    * delegates the "due?" + "ship" logic to
      :func:`send_due_subscriptions`, which handles per-row errors
      itself,
    * logs the per-tick counter dict at info level so the operator
      can grep for ``tag_email_digest.tick`` to see what fired,
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
                log.debug("tag_email_digest.disabled")
            else:
                now_iso = datetime.now().astimezone().isoformat()
                counters = await send_due_subscriptions(now_iso)
                if counters.get("sent", 0) or counters.get("errors", 0):
                    log.info(
                        "tag_email_digest.cycle",
                        worker=_WORKER_NAME,
                        **counters,
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


__all__ = ["run_tag_email_digest_worker"]
