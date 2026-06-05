"""Heartbeat alert worker — turn silent workers into bell notifications.

Periodically invokes :func:`app.worker_heartbeat_monitor.check_heartbeats`
and, for every actionable alert the monitor returns, pushes a
``worker-down`` notification via :func:`app.notifications.push` so the
operator's bell (and the SSE channel that feeds it) surface the outage
the same way capture-stopped and ocr-backlog already do.

Why a worker?
-------------
The monitor itself is a synchronous-looking async function that scans
the heartbeat table; somebody has to schedule it. Wrapping it in
:class:`app.workers._bases.BackfillRunner` gives us:

* A 10-minute poll cadence with cancellation support.
* The standard worker-down kv toggle pattern (``heartbeat_alerts_enabled``
  defaults to ``"1"``; operators flip it off when they're already aware
  of an outage and don't want bell noise).
* Free heartbeat reporting via :func:`app.workers.heartbeat.beat`, so
  the alert worker itself shows up on ``/admin/health`` — the watcher
  is watched.

Dedupe is handled inside the monitor module (per-worker
``last_alerted_at_*`` rows in ``kv_settings``). The worker just trusts
the ``should_alert`` flag on each :class:`HeartbeatAlert` and only
pushes when it's ``True``. When the kv toggle is off, the worker still
ticks (so its heartbeat stays fresh and ``/admin/health`` shows it as
green) but it skips the scan entirely.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from app.logging_setup import get_logger
from app.notifications import push as push_notification
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.worker_heartbeat_monitor import (
    HeartbeatAlert,
    check_heartbeats,
    mark_alerted,
)
from app.workers._bases import BackfillRunner

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.heartbeat_alert_worker")


POLL_INTERVAL_SECONDS: Final[int] = 600
"""Ten-minute cadence — see module docstring for the trade-off."""


_KV_ENABLED: Final[str] = "heartbeat_alerts_enabled"
"""kv_settings row toggling the whole worker. Default ``"1"`` (on)."""


_SENTINEL: object = object()
"""Single hashable token reused as the runner key — see auto_pin_worker."""


_NOTIFICATION_KIND: Final[str] = "worker-down"
"""Notification kind used by the bell UI to pick an icon and group rows."""


_NOTIFICATION_SEVERITY: Final[str] = "warn"
"""Severity passed to :func:`app.notifications.push`. ``warn`` not
``error`` because a stopped non-critical worker (e.g. weekly rollup)
shouldn't render with the same urgency as a database failure."""


async def _enabled_getter() -> bool:
    """Return whether alerting is currently turned on.

    Default ``True`` — heartbeat alerts are the kind of feature an
    operator should explicitly opt out of, not in to. A malformed value
    in the kv row also reads as enabled so a typo can never silence
    the safety net.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return True
    return raw.strip() != "0"


async def _list_missing() -> list[Any]:
    """Return ``[_SENTINEL]`` while alerting is enabled, ``[]`` otherwise.

    Mirrors :mod:`app.workers.auto_pin_worker` — the runner expects a
    list of hashable keys and a single-loop job hands it the same
    sentinel every tick.
    """
    enabled = await _enabled_getter()
    return [_SENTINEL] if enabled else []


async def _build_one(_key: Any) -> dict[str, Any] | None:
    """Run one scan + push for each actionable alert.

    The monitor is the source of truth for "is this worker silent?"
    and for the dedupe flag — we only translate the structured rows
    into bell notifications. Returning the summary dict tells the
    runner to increment ``built`` so the cycle log line carries useful
    counters; returning ``None`` would silently skip them.
    """
    alerts = await check_heartbeats()
    actionable: list[HeartbeatAlert] = [a for a in alerts if a["should_alert"]]
    if not actionable:
        log.debug(
            "heartbeat_alert_worker.tick.no_alerts",
            scanned=len(alerts),
        )
        return None

    pushed = 0
    for alert in actionable:
        title, body = _format_message(alert)
        try:
            await push_notification(
                kind=_NOTIFICATION_KIND,
                title=title,
                body=body,
                link="/admin/heartbeat-alerts",
                severity=_NOTIFICATION_SEVERITY,
            )
        except Exception as exc:
            log.warning(
                "heartbeat_alert_worker.push_failed",
                worker=alert["worker"],
                error=str(exc),
            )
            continue
        try:
            await mark_alerted(alert["worker"])
        except Exception as exc:
            log.warning(
                "heartbeat_alert_worker.mark_failed",
                worker=alert["worker"],
                error=str(exc),
            )
        pushed += 1

    log.info(
        "heartbeat_alert_worker.tick",
        scanned=len(alerts),
        actionable=len(actionable),
        pushed=pushed,
    )
    return {"scanned": len(alerts), "pushed": pushed}


def _format_message(alert: HeartbeatAlert) -> tuple[str, str]:
    """Render a single-line title + multi-line body for the bell row.

    Title carries the worker name plus a short outage descriptor so the
    bell list is scannable; the body has the numerical detail (expected
    cadence vs measured gap) so the operator can decide whether to
    intervene without leaving the bell.
    """
    worker = alert["worker"]
    expected = alert["expected_poll_seconds"]
    gap = alert["gap_seconds"]

    if gap is None:
        title = f"Worker {worker!r} has not reported a heartbeat"
        body = (
            f"Expected poll cadence: every {expected}s. "
            "No heartbeat row found — the worker may not have started, "
            "or it crashed before its first tick."
        )
    else:
        title = f"Worker {worker!r} is silent for {int(gap)}s"
        body = (
            f"Expected poll cadence: every {expected}s. "
            f"Last heartbeat at {alert['last_beat_at']} "
            f"({int(gap)}s ago, threshold {int(alert['threshold_seconds'])}s)."
        )
    return title, body


async def run_heartbeat_alert_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan entry point — wraps a :class:`BackfillRunner`."""
    runner = BackfillRunner(
        name="heartbeat-alert-worker",
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_list_missing,
        build_one=_build_one,
    )
    await runner.run(stop_event)


__all__ = [
    "POLL_INTERVAL_SECONDS",
    "run_heartbeat_alert_worker",
]
