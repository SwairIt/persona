"""Worker heartbeat monitor — surface workers that have gone silent.

Every background loop in Persona calls :func:`app.workers.heartbeat.beat`
at the top of each iteration. That gives us a ``last_run_at`` timestamp
per worker; this module turns those timestamps into actionable alerts.

For each worker in :data:`EXPECTED_POLL_INTERVALS` we look up the most
recent heartbeat row, compute ``gap = now - max(beat_at)``, and flag a
worker when ``gap > threshold_multiplier * expected_poll_seconds``. A
worker missing from the heartbeat table entirely (e.g. it crashed
before its first ``beat()`` call) also counts as ``stopped`` — the gap
is reported as ``None`` and the row is still appended to the alert
list so the dashboard surfaces it.

Dedupe policy
-------------
The check is invoked from a long-running worker (see
:mod:`app.workers.heartbeat_alert_worker`) every ten minutes. Without
dedupe a single stopped capture-loop would emit six notifications per
hour. Each worker carries its own ``last_alerted_at_<name>`` row in
``kv_settings``; a fresh alert is only emitted when the previous one
is older than :data:`_DEDUPE_WINDOW_SECONDS` (one hour).

Failure policy
--------------
``check_heartbeats`` opens a single connection, runs a parametrised
``SELECT MAX(last_run_at) FROM worker_heartbeat WHERE name = ?`` per
worker, and tolerates malformed timestamps by treating the worker as
"never reported". DB errors propagate to the caller — the alert worker
catches and logs them.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.storage.time import iso, parse_iso

log = get_logger("persona.worker_heartbeat_monitor")


EXPECTED_POLL_INTERVALS: Final[dict[str, int]] = {
    # Sub-second / few-second loops — capture, OCR, audio, embeddings.
    "capture-loop": 5,
    "audio-worker": 5,
    "ocr-worker": 5,
    "embeddings-worker": 10,
    "clipboard-worker": 5,
    "inbox-worker": 30,
    # 1-2 minute loops.
    "tag-rule-worker": 120,
    "webhook-retry-worker": 60,
    # 5 minute loops.
    "saved-search-alert": 300,
    "auto-pin-worker": 300,
    "app-budget-worker": 300,
    # 10 minute loops.
    "alt-text-worker": 600,
    "long-read-worker": 600,
    "hourly-card-worker": 600,
    "card-enrichment-worker": 600,
    # 15-30 minute loops.
    "auto-translate-worker": 900,
    "ai-reminders-worker": 1800,
    "audit-log-rotation-worker": 1800,
    "audio-merge-worker": 1800,
    "auto-backup-scheduler": 1800,
    "capture-session-worker": 1800,
    "daily-email-scheduler": 1800,
    "daily-pin-enrichment-worker": 1800,
    "daily-pin-worker": 1800,
    "day-end-summary-scheduler": 1800,
    "digest-scheduler": 600,
    "email-weekly-digest": 1800,
    "entity-extractor-worker": 1800,
    "memory-of-day": 1800,
    "s3-sync-worker": 1800,
    "smart-dedup-worker": 1800,
    "url-time-worker": 1800,
    "weekly-digest-scheduler": 1800,
    "weekly-stats-email-scheduler": 1800,
    # Hourly loops.
    "monthly-digest-scheduler": 3600,
    "obsidian-sync-worker": 3600,
    "weekly-card-worker": 3600,
    "weekly-highlights-worker": 3600,
    "weekly-rollup-worker": 3600,
}
"""Per-worker expected polling cadence in seconds.

Each entry mirrors the ``POLL_INTERVAL_SECONDS`` constant declared in
the corresponding worker module. Kept in one place so the monitor has
a single source of truth — when a worker's cadence changes upstream,
update this dict in the same commit.
"""


_DEDUPE_WINDOW_SECONDS: Final[float] = 3600.0
"""Minimum seconds between two consecutive alerts for the same worker.

Set to one hour: long enough that a stopped worker doesn't spam the
bell, short enough that a still-down worker is re-surfaced within the
same shift so the operator notices on their next glance.
"""


_KV_LAST_ALERTED_PREFIX: Final[str] = "last_alerted_at_"
"""Per-worker dedupe row prefix used in ``kv_settings``.

Clearing the dedupe state from the admin page wipes every row whose
key starts with this prefix.
"""


class HeartbeatAlert(TypedDict):
    """One row of the alert list returned by :func:`check_heartbeats`."""

    worker: str
    expected_poll_seconds: int
    gap_seconds: float | None
    threshold_seconds: float
    last_beat_at: str | None
    last_alerted_at: str | None
    should_alert: bool


async def check_heartbeats(
    threshold_multiplier: float = 3.0,
) -> list[HeartbeatAlert]:
    """Scan every tracked worker; return rows whose gap exceeds the threshold.

    Args:
        threshold_multiplier: Multiplier applied to each worker's
            ``EXPECTED_POLL_INTERVALS`` value. A worker is considered
            "down" when the gap since its most recent heartbeat exceeds
            ``threshold_multiplier * expected_poll_seconds``. Defaults
            to ``3.0`` — enough slack to forgive one missed tick plus
            the inevitable jitter of an asyncio loop under load.

    Returns:
        A list of :class:`HeartbeatAlert` rows, one per worker that
        currently exceeds its threshold (or has never reported at
        all). Each row includes the dedupe state so the caller can
        decide whether to push a fresh notification or stay silent.
        Workers that are healthy are *not* in the list — the alert
        worker treats an empty list as "nothing to do".
    """
    if threshold_multiplier <= 0:
        msg = (
            "threshold_multiplier must be > 0, "
            f"got {threshold_multiplier!r}"
        )
        raise ValueError(msg)

    now = datetime.now(UTC)
    alerts: list[HeartbeatAlert] = []

    async with get_connection() as conn:
        for worker, expected_poll_seconds in EXPECTED_POLL_INTERVALS.items():
            threshold_seconds = threshold_multiplier * float(expected_poll_seconds)

            cursor = await conn.execute(
                "SELECT MAX(last_run_at) AS max_beat FROM worker_heartbeat WHERE name = ?",
                (worker,),
            )
            row = await cursor.fetchone()
            raw_last_beat = None if row is None else row["max_beat"]

            last_beat_iso: str | None
            gap_seconds: float | None
            if raw_last_beat is None:
                last_beat_iso = None
                gap_seconds = None
                over_threshold = True
            else:
                last_beat_iso = str(raw_last_beat)
                parsed = _parse_beat(last_beat_iso)
                if parsed is None:
                    gap_seconds = None
                    over_threshold = True
                else:
                    gap_seconds = max(0.0, (now - parsed).total_seconds())
                    over_threshold = gap_seconds > threshold_seconds

            if not over_threshold:
                continue

            last_alerted_raw = await get_kv(
                conn, _KV_LAST_ALERTED_PREFIX + worker
            )
            last_alerted = _parse_beat(last_alerted_raw)
            should_alert = (
                last_alerted is None
                or (now - last_alerted).total_seconds() >= _DEDUPE_WINDOW_SECONDS
            )

            alerts.append(
                HeartbeatAlert(
                    worker=worker,
                    expected_poll_seconds=expected_poll_seconds,
                    gap_seconds=(
                        None if gap_seconds is None else round(gap_seconds, 3)
                    ),
                    threshold_seconds=round(threshold_seconds, 3),
                    last_beat_at=last_beat_iso,
                    last_alerted_at=(
                        None if last_alerted_raw is None else str(last_alerted_raw)
                    ),
                    should_alert=should_alert,
                )
            )

    log.info(
        "worker_heartbeat_monitor.scan",
        threshold_multiplier=threshold_multiplier,
        alerts_total=len(alerts),
        alerts_actionable=sum(1 for a in alerts if a["should_alert"]),
    )
    return alerts


async def mark_alerted(worker: str, at: datetime | None = None) -> None:
    """Record that ``worker`` was just alerted on.

    Writes ``last_alerted_at_<worker>`` into ``kv_settings`` so the
    next :func:`check_heartbeats` invocation within the dedupe window
    returns ``should_alert=False`` for the same row. Called from the
    alert worker right after :func:`app.notifications.push` succeeds.
    """
    stamp = at if at is not None else datetime.now(UTC)
    async with get_connection() as conn:
        await set_kv(conn, _KV_LAST_ALERTED_PREFIX + worker, iso(stamp))
    log.info(
        "worker_heartbeat_monitor.mark_alerted",
        worker=worker,
        at=iso(stamp),
    )


async def clear_dedupe() -> int:
    """Drop every ``last_alerted_at_*`` row; return how many were removed.

    Exposed to operators via the admin page's "clear dedupe" button.
    After a clear, the next scan re-alerts on every still-down worker
    even if it was alerted on minutes ago — useful when an operator
    silenced a flood of notifications and now wants a fresh signal.
    """
    pattern = _KV_LAST_ALERTED_PREFIX + "%"
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM kv_settings WHERE key LIKE ?",
            (pattern,),
        )
        await conn.commit()
        removed = int(cursor.rowcount or 0)
    log.info("worker_heartbeat_monitor.clear_dedupe", removed=removed)
    return removed


def _parse_beat(raw: str | None) -> datetime | None:
    """Parse a stored ISO timestamp into an aware UTC datetime.

    Returns ``None`` when ``raw`` is empty or unparseable. Naive values
    are assumed UTC — the storage layer only ever writes UTC strings
    via :func:`app.storage.time.iso`, so a naive read here means the
    column was written before that convention was enforced and the
    "treat as UTC" assumption is the same one the dashboard uses.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        parsed = parse_iso(text)
    except ValueError:
        log.warning("worker_heartbeat_monitor.parse_failed", value=text[:80])
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


__all__ = [
    "EXPECTED_POLL_INTERVALS",
    "HeartbeatAlert",
    "check_heartbeats",
    "clear_dedupe",
    "mark_alerted",
]
