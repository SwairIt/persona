"""Polling worker that drains the webhook retry queue.

Wakes once a minute and calls :func:`app.webhook_retry.process_queue`,
which picks every row whose ``next_attempt_at`` is in the past and
replays the HTTP POST. The cadence is deliberately coarse — the
exponential-backoff schedule is measured in minutes, so polling more
often only burns CPU without delivering events any sooner.

Per-tick exceptions are caught and logged so a transient SQLite or
DNS hiccup never escapes the worker loop. Cancellation is honoured
promptly via the shared ``CaptureController.stop_event``.
"""

from __future__ import annotations

import asyncio
from typing import Final

from app.logging_setup import get_logger
from app.webhook_retry import process_queue
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

log = get_logger("persona.webhook.retry")

POLL_INTERVAL_SECONDS: Final[float] = 60.0
"""One-minute polling cadence — matches the smallest backoff bucket
(``2 ** 1 == 2`` minutes) so the worst-case extra wait between a row
becoming due and the worker noticing is one minute."""

_HEARTBEAT_NAME: Final[str] = "webhook-retry"


async def run_webhook_retry_worker(
    controller: CaptureController | None = None,
) -> None:
    """Continuously drain the retry queue until ``stop_event`` fires.

    Mirrors the structure of every other worker in this package:
    heartbeat, do-one-tick, sleep-with-stop-event-bailout. The
    ``controller`` argument is optional so callers in tests can pass an
    isolated instance instead of the process-wide singleton.
    """
    ctrl = controller or get_controller()
    log.info("webhook.retry.started", poll_seconds=POLL_INTERVAL_SECONDS)

    while not ctrl.stop_event.is_set():
        await beat(_HEARTBEAT_NAME)
        try:
            await process_queue()
        except asyncio.CancelledError:
            log.info("webhook.retry.cancelled")
            raise
        except Exception as exc:  # defensive: keep the loop alive on per-tick errors
            log.exception("webhook.retry.tick_failed", error=str(exc))

        try:
            await asyncio.wait_for(
                ctrl.stop_event.wait(),
                timeout=POLL_INTERVAL_SECONDS,
            )
        except TimeoutError:
            continue

    log.info("webhook.retry.stopped")


__all__ = ["POLL_INTERVAL_SECONDS", "run_webhook_retry_worker"]
