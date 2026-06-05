"""Weekly LLM rollup worker (v1.27).

Polls every hour. When the operator has opted in via
``kv_settings.weekly_llm_rollup_enabled = '1'`` the worker looks back
four ISO weeks and asks :func:`app.llm.weekly_rollup.rollup_week` to
fill the ``llm_summary`` column on any ``weekly_card`` row that still
has it as ``NULL``.

Disabled by default — the kv flag is absent on a fresh install, which
this module treats as ``'0'``. Flipping the flag to ``'1'`` via
``/api/weekly-rollup`` is the only way to start spending tokens on the
weekly narrative.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.llm.weekly_rollup import rollup_week
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers._bases import BackfillRunner

if TYPE_CHECKING:
    import asyncio

log = get_logger("persona.weekly_rollup_worker")

POLL_INTERVAL_SECONDS: int = 3600
LOOKBACK_WEEKS: int = 4
_KV_FLAG: str = "weekly_llm_rollup_enabled"
_WORKER_NAME: str = "weekly-rollup-worker"


async def _is_enabled() -> bool:
    """Return ``True`` only when the kv row is the literal string ``'1'``."""
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_FLAG)
    return (raw or "0").strip() == "1"


async def _select_pending() -> list[str]:
    """Return up to ``LOOKBACK_WEEKS`` week_start strings still NULL.

    Returns an empty list when the feature is disabled — the
    BackfillRunner then just sleeps until the next tick. The query
    orders by most-recent first so a freshly enabled feature surfaces
    the user's current week before backfilling older ones.
    """
    if not await _is_enabled():
        return []
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT week_start FROM weekly_card "
            "WHERE llm_summary IS NULL "
            "ORDER BY week_start DESC LIMIT ?",
            (LOOKBACK_WEEKS,),
        )
        rows = await cursor.fetchall()
    return [str(row["week_start"]) for row in rows]


async def _build_one(week_start_iso: str) -> dict[str, str]:
    """Wrap rollup_week so the runner sees an empty dict on no-op statuses.

    The runner increments its built-counter only on truthy results, so
    we return the dict on real work and ``{}`` for status-skips. Logs
    the missing-config case once per tick so the operator notices.
    """
    result = await rollup_week(week_start_iso)
    status = result["status"]
    if status == "missing_config":
        log.info(
            "weekly_rollup_worker.missing_config",
            week_start=week_start_iso,
        )
        return {}
    if status in {"already_done", "no_data", "error"}:
        return {}
    return result


async def run_weekly_rollup_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan entry point — registers a :class:`BackfillRunner`."""
    runner = BackfillRunner(
        name=_WORKER_NAME,
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_select_pending,
        build_one=_build_one,
    )
    await runner.run(stop_event)


__all__ = [
    "LOOKBACK_WEEKS",
    "POLL_INTERVAL_SECONDS",
    "run_weekly_rollup_worker",
]
