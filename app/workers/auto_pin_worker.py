"""Auto-pin worker — periodic driver for :mod:`app.auto_pin_engine`.

Reuses :class:`app.workers._bases.BackfillRunner` so the lifespan
coordinator picks it up via the usual ``run_<name>_worker`` entry
point and the heartbeat dashboard shows it next to the other workers.

Cadence is 5 minutes — long enough that a slow regex over a 100-row
batch cannot pile up behind itself, short enough that a freshly
captured shot is auto-pinned within roughly one OCR cycle plus this
interval. Cheap on idle laptops because :func:`run_auto_pins` exits
early when no rules exist.

The ``list_missing`` callback returns a one-element sentinel list
whenever at least one rule is enabled and an empty list otherwise.
That keeps :class:`BackfillRunner`'s "build N missing rows" contract
intact (every tick we either build one engine run or none) while
avoiding a wasted SQL roundtrip on operators who haven't created any
auto-pin rules yet.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.auto_pin_engine import DEFAULT_DAILY_CAP, run_auto_pins
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.workers._bases import BackfillRunner

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.auto_pin_worker")


POLL_INTERVAL_SECONDS: int = 300
"""Five-minute cadence. See module docstring for the trade-off."""


DAILY_CAP: int = DEFAULT_DAILY_CAP
"""Per-day cap on auto-pins. Mirrors the engine default for clarity."""


_SENTINEL: object = object()
"""Single hashable token reused as the ``BackfillRunner`` key.

The runner expects a list of hashable keys; auto-pin is a single
loop-style job so we hand it the same sentinel every tick.
"""


async def _list_missing() -> list[Any]:
    """Return ``[_SENTINEL]`` iff at least one enabled rule exists.

    Cheap one-row probe — avoids opening the engine's connection on
    instances that have no auto-pin rules configured. The probe and
    the engine run open separate connections; that's fine because
    auto-pin is the only writer of ``auto_pin_*`` tables and the
    engine commits idempotently per rule.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT 1 FROM auto_pin_rule WHERE enabled = 1 LIMIT 1",
        )
        row = await cursor.fetchone()
    return [_SENTINEL] if row is not None else []


async def _build_one(_key: Any) -> dict[str, Any] | None:
    """Run one engine tick. Returns the summary dict on a real run.

    Returning ``None`` would tell :class:`BackfillRunner` "nothing
    built" and skip the cycle log line; we always have at least the
    `rules_processed` counter to report when we reach here, so we
    return the dict unconditionally.
    """
    async with get_connection() as conn:
        summary = await run_auto_pins(conn, daily_cap=DAILY_CAP)
    log.info(
        "auto_pin_worker.tick",
        rules_processed=summary["rules_processed"],
        shots_pinned=summary["shots_pinned"],
        daily_cap_hit=summary["daily_cap_hit"],
    )
    return dict(summary)


async def run_auto_pin_worker(stop_event: asyncio.Event | None = None) -> None:
    """Lifespan entry point — registers a :class:`BackfillRunner`."""
    runner = BackfillRunner(
        name="auto-pin-worker",
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_list_missing,
        build_one=_build_one,
    )
    await runner.run(stop_event)


__all__ = [
    "DAILY_CAP",
    "POLL_INTERVAL_SECONDS",
    "run_auto_pin_worker",
]
