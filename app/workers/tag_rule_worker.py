"""Background worker for the tag-rule auto-applier.

Wakes every :data:`POLL_INTERVAL_SECONDS`, calls
:func:`app.tag_rule_engine.run_rules_against_new_shots`, then sleeps on
the controller's stop event so the worker tears down promptly during
process shutdown.

Heartbeat key is ``tag-rule-worker``; the admin health page surfaces it
beside the other workers via :func:`app.workers.heartbeat.get_all`. The
loop never raises — exceptions inside a tick are logged with
``log.exception`` and the worker keeps marching.

Wired-up note: this module exports :func:`run_tag_rule_worker` only. The
lifespan coordinator picks up the new function automatically (per the
project rule that workers must not modify ``app/web/main.py`` directly)
— there is nothing to register here.
"""

from __future__ import annotations

import asyncio

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.tag_rule_engine import run_rules_against_new_shots
from app.workers.control import CaptureController, get_controller
from app.workers.heartbeat import beat

log = get_logger("persona.tag_rule_worker")


POLL_INTERVAL_SECONDS: float = 120.0
"""Two-minute cadence.

Long enough that the SQL scan cannot pile up behind itself even with a
backlog of rules, short enough that a newly captured shot is auto-tagged
within roughly one OCR cycle plus this interval — well inside the
"feels live" window for the dashboard.
"""


HEARTBEAT_NAME: str = "tag-rule-worker"


async def run_tag_rule_worker(controller: CaptureController | None = None) -> None:
    """Drive the auto-applier loop until ``stop_event`` fires.

    The tick is idempotent: the engine module advances each rule's
    watermark only after a successful commit, so a hard stop between
    ticks (Ctrl-C, supervisor restart) replays at most the in-flight
    batch on the next start — which is harmless because
    ``INSERT OR IGNORE`` on ``screenshot_tags`` makes re-tagging a
    no-op.
    """
    ctrl = controller or get_controller()
    log.info("tag_rule_worker.started", poll_seconds=POLL_INTERVAL_SECONDS)

    while not ctrl.stop_event.is_set():
        await beat(HEARTBEAT_NAME)
        try:
            async with get_connection() as conn:
                summary = await run_rules_against_new_shots(conn)
            if summary["rules_processed"]:
                # Only log a summary line when at least one rule ran;
                # otherwise the journal fills with empty ticks from
                # users who haven't created any rules yet.
                log.info(
                    "tag_rule_worker.tick",
                    rules_processed=summary["rules_processed"],
                    screenshots_scanned=summary["screenshots_scanned"],
                    tags_added=summary["tags_added"],
                )
        except asyncio.CancelledError:
            log.info("tag_rule_worker.cancelled")
            raise
        except Exception as exc:
            # ``log.exception`` records the traceback automatically;
            # the loop continues so a transient DB hiccup does not take
            # the whole worker down until the next process restart.
            log.exception("tag_rule_worker.tick_failed", error=str(exc))

        try:
            await asyncio.wait_for(
                ctrl.stop_event.wait(),
                timeout=POLL_INTERVAL_SECONDS,
            )
        except TimeoutError:
            continue

    log.info("tag_rule_worker.stopped")


__all__ = [
    "HEARTBEAT_NAME",
    "POLL_INTERVAL_SECONDS",
    "run_tag_rule_worker",
]
