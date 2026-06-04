"""LLM enrichment worker for hourly cards (v1.14, refactored onto BackfillRunner in v1.26).

Polls every 10 minutes; when the operator has opted in via
``kv_settings.hourly_card_llm_enrichment_enabled = '1'`` the worker
picks the three most recent un-enriched cards and runs them through
:func:`app.llm.card_enricher.enrich_card` sequentially so the
provider's rate limit is respected.

Disabled by default — the kv flag is absent on a fresh install, which
this module treats as ``'0'``. Flipping the flag to ``'1'`` via
``/api/cards/enrichment`` is the only way to start spending tokens on
narrative.
"""

from __future__ import annotations

import asyncio

from app.llm.card_enricher import enrich_card
from app.logging_setup import get_logger
from app.settings.effective import get_effective_bool
from app.storage.db import get_connection
from app.workers._bases import BackfillRunner

log = get_logger("persona.card_enrichment_worker")

POLL_INTERVAL_SECONDS: int = 600
_KV_FLAG: str = "hourly_card_llm_enrichment_enabled"
_BATCH_LIMIT: int = 3
_WORKER_NAME: str = "card-enrichment-worker"


async def _select_pending() -> list[str]:
    """Return up to _BATCH_LIMIT hour_starts that still need enrichment.

    Returns an empty list when the feature is disabled — the
    BackfillRunner then just sleeps until the next tick.
    """
    if not await get_effective_bool(_KV_FLAG, default=False):
        return []
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT hour_start FROM hourly_card "
            "WHERE llm_enriched = 0 "
            "ORDER BY hour_start DESC LIMIT ?",
            (_BATCH_LIMIT,),
        )
        rows = await cursor.fetchall()
    return [str(row["hour_start"]) for row in rows]


async def _enrich_one(hour_start: str) -> dict[str, object]:
    """Wrap enrich_card so the runner sees None on no-op statuses.

    The runner increments its built-counter only on truthy results, so
    we return the dict on real work and None for status-skips. Logs
    the missing-config case once per tick so the operator notices.
    """
    result = await enrich_card(hour_start)
    if result["status"] == "missing_config":
        log.info("card_enrichment_worker.missing_config", hour=hour_start)
        return {}
    if result["status"] == "already_enriched":
        return {}
    if result["status"] == "ok":
        return result
    return {}


async def run_card_enrichment_worker(
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


__all__ = ["POLL_INTERVAL_SECONDS", "run_card_enrichment_worker"]
