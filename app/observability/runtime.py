"""Low-overhead event-loop, SQLite-lock and queue telemetry.

The public liveness probe never imports this module. Metrics are process-local
and bounded; queue depths are queried only by the authenticated owner health
endpoint.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from dataclasses import dataclass
from typing import Final

_WINDOW_SECONDS: Final[float] = 300.0
_MAX_EVENT_LOOP_SAMPLES: Final[int] = 1_200
# A busy SQLite application can begin far more than four writes per second.
# Keep enough bounded history for 200 attempts/s across the advertised window.
_MAX_DB_WRITE_SAMPLES: Final[int] = 60_000


@dataclass(frozen=True, slots=True)
class _Sample:
    observed_at: float
    value_ms: float


@dataclass(slots=True)
class _Counters:
    db_write_attempts: int = 0
    db_write_failures: int = 0


_event_loop_lag: deque[_Sample] = deque(maxlen=_MAX_EVENT_LOOP_SAMPLES)
_db_write_wait: deque[_Sample] = deque(maxlen=_MAX_DB_WRITE_SAMPLES)
_counters = _Counters()

_QUEUE_QUERIES: Final[dict[str, tuple[str, str]]] = {
    "llm": (
        "llm_job",
        """
        SELECT status, COUNT(*) AS n FROM llm_job
        WHERE status IN ('pending', 'streaming') GROUP BY status
        """,
    ),
    "remote_browser": (
        "remote_browser_job",
        """
        SELECT status, COUNT(*) AS n FROM remote_browser_job
        WHERE status IN ('pending', 'claimed') GROUP BY status
        """,
    ),
    "autowake": (
        "autowake_outbox",
        """
        SELECT status, COUNT(*) AS n FROM autowake_outbox
        WHERE status IN ('pending', 'leased', 'retry') GROUP BY status
        """,
    ),
    "telegram_inbox": (
        "telegram_update_inbox",
        """
        SELECT status, COUNT(*) AS n FROM telegram_update_inbox
        WHERE status = 'processing' GROUP BY status
        """,
    ),
    "memory_projection": (
        "memory_projection_outbox",
        """
        SELECT status, COUNT(*) AS n FROM memory_projection_outbox
        WHERE status IN ('pending', 'leased', 'retry') GROUP BY status
        """,
    ),
}


def _record(samples: deque[_Sample], seconds: float) -> None:
    value_ms = max(0.0, float(seconds) * 1_000.0)
    samples.append(_Sample(time.monotonic(), value_ms))


def record_db_write_wait(seconds: float, *, acquired: bool) -> None:
    """Record how long ``BEGIN IMMEDIATE`` waited for the writer lock."""

    _counters.db_write_attempts += 1
    if not acquired:
        _counters.db_write_failures += 1
    _record(_db_write_wait, seconds)


def _summary(samples: deque[_Sample], *, now: float) -> dict[str, float | int | None]:
    cutoff = now - _WINDOW_SECONDS
    values = [sample.value_ms for sample in samples if sample.observed_at >= cutoff]
    if not values:
        return {
            "samples": 0,
            "last_ms": None,
            "p95_ms": None,
            "max_ms": None,
        }
    ordered = sorted(values)
    percentile_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "samples": len(values),
        "last_ms": round(values[-1], 3),
        "p95_ms": round(ordered[percentile_index], 3),
        "max_ms": round(ordered[-1], 3),
    }


def runtime_snapshot() -> dict[str, object]:
    """Return a bounded five-minute telemetry snapshot."""

    now = time.monotonic()
    return {
        "window_seconds": int(_WINDOW_SECONDS),
        "event_loop_lag": _summary(_event_loop_lag, now=now),
        "db_write_lock_wait": {
            **_summary(_db_write_wait, now=now),
            "attempts_total": _counters.db_write_attempts,
            "failures_total": _counters.db_write_failures,
        },
    }


async def monitor_event_loop(
    stop_event: asyncio.Event | None = None,
    *,
    interval_seconds: float = 1.0,
) -> None:
    """Continuously sample scheduler delay until cancelled or stopped."""

    if not 0.05 <= interval_seconds <= 60.0:
        raise ValueError("interval_seconds must be in 0.05..60")
    stop = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + interval_seconds
    while not stop.is_set():
        timeout = max(0.0, deadline - loop.time())
        try:
            await asyncio.wait_for(stop.wait(), timeout=timeout)
            break
        except TimeoutError:
            observed = loop.time()
            _record(_event_loop_lag, observed - deadline)
            deadline += interval_seconds
            if observed - deadline > interval_seconds * 5:
                deadline = observed + interval_seconds


async def queue_depths() -> dict[str, dict[str, int]]:
    """Read all durable queue depths with one short SQLite connection."""

    from app.storage.db import get_connection  # noqa: PLC0415

    result: dict[str, dict[str, int]] = {}
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        existing = {str(row["name"]) for row in await cursor.fetchall()}
        for queue_name, (table_name, query) in _QUEUE_QUERIES.items():
            if table_name not in existing:
                result[queue_name] = {"unavailable": 1}
                continue
            status_cursor = await conn.execute(query)
            result[queue_name] = {
                str(row["status"]): int(row["n"])
                for row in await status_cursor.fetchall()
            }
    return result


def _reset_for_tests() -> None:
    _event_loop_lag.clear()
    _db_write_wait.clear()
    _counters.db_write_attempts = 0
    _counters.db_write_failures = 0


__all__ = [
    "monitor_event_loop",
    "queue_depths",
    "record_db_write_wait",
    "runtime_snapshot",
]
