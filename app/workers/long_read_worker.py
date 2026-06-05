"""Long-read detection worker (v1.39).

Wraps :func:`app.long_read_detector.detect_long_reads` in a
:class:`app.workers._bases.BackfillRunner` so the lifespan task layout
stays uniform with the other backfill-style workers (hourly card,
daily-pin enrichment, …).

The runner is gated by the ``long_read_detection_enabled`` kv flag,
which defaults to ``1`` — the feature is opt-out rather than opt-in,
matching the other always-on bookkeeping workers like
``capture_weekly_trend``. When the flag is disabled
``_list_missing`` returns an empty list and the runner just sleeps
until the next tick.

The :class:`BackfillRunner` expects ``list_missing`` to return a list
of keys for ``build_one`` to consume. A periodic detector has no such
key list — we want exactly one detection pass per tick — so we use
the single sentinel ``["tick"]`` and ignore the argument inside
``build_one``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from app.logging_setup import get_logger
from app.long_read_detector import LongReadDetectionResult, detect_long_reads
from app.settings.effective import get_effective_bool
from app.workers._bases import BackfillRunner

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.workers.long_read")

POLL_INTERVAL_SECONDS: Final[int] = 600
LOOKBACK_MINUTES: Final[int] = 60
MIN_DURATION_MINUTES: Final[int] = 5

_KV_FLAG: Final[str] = "long_read_detection_enabled"
_WORKER_NAME: Final[str] = "long-read-worker"
_TICK_SENTINEL: Final[str] = "tick"


async def _list_missing() -> list[str]:
    """Return ``["tick"]`` while the feature is enabled, otherwise ``[]``.

    The kv flag defaults to ``True`` so a fresh install starts
    auto-bookmarking immediately; an operator who wants to silence the
    detector can set ``long_read_detection_enabled = '0'``.
    """
    enabled = await get_effective_bool(_KV_FLAG, default=True)
    if not enabled:
        return []
    return [_TICK_SENTINEL]


async def _build_one(_key: str) -> LongReadDetectionResult | None:
    """Invoke the detector once. Returns ``None`` when nothing was inserted.

    :class:`BackfillRunner` increments its ``built`` counter only on
    truthy results — returning ``None`` for empty ticks keeps the log
    line ``worker.cycle`` aligned with rows actually written rather
    than tick count.
    """
    result = await detect_long_reads(
        lookback_minutes=LOOKBACK_MINUTES,
        min_duration_minutes=MIN_DURATION_MINUTES,
    )
    if result["inserted"] == 0:
        return None
    return result


async def run_long_read_worker(stop_event: asyncio.Event | None = None) -> None:
    """Lifespan entry point — registers a :class:`BackfillRunner`."""
    runner = BackfillRunner(
        name=_WORKER_NAME,
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_list_missing,
        build_one=_build_one,
    )
    await runner.run(stop_event)


__all__ = [
    "LOOKBACK_MINUTES",
    "MIN_DURATION_MINUTES",
    "POLL_INTERVAL_SECONDS",
    "run_long_read_worker",
]
