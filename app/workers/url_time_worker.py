"""URL-time aggregation worker (v1.50).

Periodically refreshes the ``url_time_aggregate`` table for the
trailing three local days.  The current day is recomputed because new
screenshots stream in continuously; the two prior days are kept on
the rotation so a daemon that was stopped overnight catches up
without operator intervention.

Gated by the ``url_time_tracking_enabled`` kv flag, defaulting to
``True`` — the bookkeeping is cheap and the feature is opt-out rather
than opt-in.  Disabling the flag at runtime is honoured at the next
poll tick: the worker just sees an empty key list and sleeps.

Layered on :class:`app.workers._bases.BackfillRunner` so the lifespan
task list in ``app/web/main.py`` doesn't need a special-case loop;
this worker plugs in the same way long_read_worker does.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Final

from app.logging_setup import get_logger
from app.settings.effective import get_effective_bool
from app.url_time_tracker import aggregate_day
from app.workers._bases import BackfillRunner

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.url_time_tracker")

POLL_INTERVAL_SECONDS: Final[int] = 1800
LOOKBACK_DAYS: Final[int] = 3

_KV_FLAG: Final[str] = "url_time_tracking_enabled"
_WORKER_NAME: Final[str] = "url-time-worker"


async def _list_missing() -> list[str]:
    """Return the last :data:`LOOKBACK_DAYS` ISO dates (newest first).

    Returning the full window every tick is deliberate — the upsert
    in :func:`app.url_time_tracker.aggregate_day` is idempotent, and
    re-running yesterday cheaply catches a daemon that was paused
    overnight without bookkeeping which days were "already done".
    """
    enabled = await get_effective_bool(_KV_FLAG, default=True)
    if not enabled:
        return []

    today = datetime.now().astimezone().date()
    return [
        (today - timedelta(days=offset)).isoformat()
        for offset in range(LOOKBACK_DAYS)
    ]


async def _build_one(day_iso: str) -> dict[str, object] | None:
    """Recompute the URL-time aggregates for a single day.

    Returns the summary dict so :class:`BackfillRunner`'s ``built``
    counter increments only when at least one row was written; an
    all-empty day contributes nothing useful to the log.
    """
    summary = await aggregate_day(day_iso)
    if summary["rows_written"] == 0:
        return None
    return dict(summary)


async def run_url_time_worker(stop_event: asyncio.Event | None = None) -> None:
    """Lifespan entry point — registers a :class:`BackfillRunner`."""
    runner = BackfillRunner(
        name=_WORKER_NAME,
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_list_missing,
        build_one=_build_one,
    )
    await runner.run(stop_event)


__all__ = [
    "LOOKBACK_DAYS",
    "POLL_INTERVAL_SECONDS",
    "run_url_time_worker",
]
