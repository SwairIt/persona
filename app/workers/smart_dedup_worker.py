"""Smart-dedup worker — periodic trivial-dup scan (v1.42).

Wraps :func:`app.smart_dedup.detect_trivial_dups` in a
:class:`app.workers._bases.BackfillRunner` so the lifespan task layout
stays uniform with the other backfill-style workers (long-read,
hourly card, daily-pin enrichment, …).

The runner is gated by the ``smart_dedup_enabled`` kv flag, which
defaults to ``1`` — the feature is opt-out rather than opt-in, matching
other always-on bookkeeping workers. When disabled,
:func:`_list_missing` returns an empty list and the runner just sleeps
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
from app.settings.effective import get_effective_bool
from app.smart_dedup import SmartDedupResult, detect_trivial_dups
from app.workers._bases import BackfillRunner

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.workers.smart_dedup")

POLL_INTERVAL_SECONDS: Final[int] = 1800
LOOKBACK_HOURS: Final[int] = 6

_KV_FLAG: Final[str] = "smart_dedup_enabled"
_WORKER_NAME: Final[str] = "smart-dedup-worker"
_TICK_SENTINEL: Final[str] = "tick"


async def _list_missing() -> list[str]:
    """Return ``["tick"]`` while the feature is enabled, otherwise ``[]``.

    The kv flag defaults to ``True`` so a fresh install starts
    suppressing trivial dupes immediately; an operator who wants to
    silence the detector can set ``smart_dedup_enabled = '0'``.
    """
    enabled = await get_effective_bool(_KV_FLAG, default=True)
    if not enabled:
        return []
    return [_TICK_SENTINEL]


async def _build_one(_key: str) -> SmartDedupResult | None:
    """Invoke the detector once. Returns ``None`` when nothing was marked.

    :class:`BackfillRunner` increments its ``built`` counter only on
    truthy results — returning ``None`` for empty ticks keeps the log
    line ``worker.cycle`` aligned with rows actually written rather
    than tick count.
    """
    result = await detect_trivial_dups(lookback_hours=LOOKBACK_HOURS)
    if result["marked"] == 0:
        return None
    return result


async def run_smart_dedup_worker(stop_event: asyncio.Event | None = None) -> None:
    """Lifespan entry point — registers a :class:`BackfillRunner`."""
    runner = BackfillRunner(
        name=_WORKER_NAME,
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_list_missing,
        build_one=_build_one,
    )
    await runner.run(stop_event)


__all__ = [
    "LOOKBACK_HOURS",
    "POLL_INTERVAL_SECONDS",
    "run_smart_dedup_worker",
]
