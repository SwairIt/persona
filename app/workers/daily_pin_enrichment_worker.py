"""LLM enrichment worker for daily pins (v1.19).

Polls every 30 minutes; when the operator has opted in via
``kv_settings.daily_pin_llm_enrichment_enabled = '1'`` the worker picks
the seven most recent un-enriched pins and runs them through
:func:`app.llm.daily_pin_enricher.enrich_pin` sequentially so the
provider's rate limit is respected.

Disabled by default — the kv flag is absent on a fresh install, which
this module treats as ``'0'``. Flipping the flag to ``'1'`` via
``/api/daily-pin/enrichment`` is the only way to start spending tokens
on daily narratives.
"""

from __future__ import annotations

import asyncio

from app.llm.daily_pin_enricher import enrich_pin
from app.logging_setup import get_logger
from app.settings.effective import get_effective_bool
from app.storage.db import get_connection
from app.workers._bases import BackfillRunner

log = get_logger("persona.daily_pin_enrichment_worker")

POLL_INTERVAL_SECONDS: int = 1800
_KV_FLAG: str = "daily_pin_llm_enrichment_enabled"
_BATCH_LIMIT: int = 7
_WORKER_NAME: str = "daily-pin-enrichment-worker"


async def _select_pending() -> list[str]:
    """Return up to ``_BATCH_LIMIT`` day_iso values that still need work.

    Returns an empty list when the feature is disabled — the
    :class:`BackfillRunner` then just sleeps until the next tick.
    """
    if not await get_effective_bool(_KV_FLAG, default=False):
        return []
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT day FROM daily_pin "
            "WHERE llm_enriched = 0 "
            "ORDER BY day DESC LIMIT ?",
            (_BATCH_LIMIT,),
        )
        rows = await cursor.fetchall()
    return [str(row["day"]) for row in rows]


async def _enrich_one(day_iso: str) -> dict[str, object] | None:
    """Wrap :func:`enrich_pin` so the runner sees ``None`` on no-op statuses.

    :class:`BackfillRunner` increments its built-counter only on truthy
    results, so we return the dict on real work and ``None`` for
    status-skips. Logs the missing-config case once per tick so the
    operator notices.
    """
    result = await enrich_pin(day_iso)
    if result["status"] == "missing_config":
        log.info("daily_pin_enrichment_worker.missing_config", day=day_iso)
        return None
    if result["status"] == "already_enriched":
        return None
    if result["status"] == "ok":
        return dict(result)
    return None


async def run_daily_pin_enrichment_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Lifespan entry point — registers a :class:`BackfillRunner`."""
    runner = BackfillRunner(
        name=_WORKER_NAME,
        poll_seconds=POLL_INTERVAL_SECONDS,
        list_missing=_select_pending,
        build_one=_enrich_one,
    )
    await runner.run(stop_event)


__all__ = ["POLL_INTERVAL_SECONDS", "run_daily_pin_enrichment_worker"]
