"""LLM enrichment worker for hourly cards (v1.14).

Polls every 10 minutes; when the operator has opted in via
``kv_settings.hourly_card_llm_enrichment_enabled = '1'`` the worker
picks the three most recent un-enriched cards and runs them through
:func:`app.llm.card_enricher.enrich_card` **sequentially** so the
provider's rate limit is respected.

Disabled by default — the kv flag is absent on a fresh install, which
this module treats as ``'0'``. Flipping the flag to ``'1'`` via
``/api/cards/enrichment`` is the only way to start spending tokens on
narrative.

The worker is import-safe and idempotent:

* If the LLM is not configured, :func:`enrich_card` returns
  ``missing_config`` and we log + skip — no crash, no retry storm.
* If a card is already ``llm_enriched = 1`` (e.g. enriched manually
  through a future admin endpoint) the inner call returns
  ``already_enriched`` and the worker moves on.
* ``asyncio.CancelledError`` propagates so the lifespan task supervisor
  can stop the worker cleanly on shutdown.
"""

from __future__ import annotations

import asyncio

from app.llm.card_enricher import enrich_card
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv
from app.workers.heartbeat import beat

log = get_logger('persona.card_enrichment_worker')

#: Poll cadence. 10 minutes matches the hourly-card builder cadence and
#: gives the operator near-real-time enrichment without hammering the
#: provider — at most ``_BATCH_LIMIT`` calls per tick.
POLL_INTERVAL_SECONDS: int = 600

#: kv_settings row that gates the worker. The string ``'1'`` enables
#: enrichment; anything else (including the row being absent) disables.
_KV_FLAG: str = 'hourly_card_llm_enrichment_enabled'

#: Maximum number of cards to enrich per cycle. Three is a deliberately
#: low cap — provider free tiers (Gemini, Groq) tolerate a few calls
#: per minute easily, and the worker catches up on a backlog over
#: subsequent ticks rather than burning the rate limit in one burst.
_BATCH_LIMIT: int = 3

_WORKER_NAME: str = 'card-enrichment-worker'


async def _is_enabled() -> bool:
    """Read the kv flag with a safe default of ``False``."""
    async with get_connection() as conn:
        value = await get_kv(conn, _KV_FLAG)
    return (value or '0').strip() == '1'


async def _select_pending() -> list[str]:
    """Return up to ``_BATCH_LIMIT`` hour_starts that still need enrichment."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            'SELECT hour_start FROM hourly_card '
            'WHERE llm_enriched = 0 '
            'ORDER BY hour_start DESC LIMIT ?',
            (_BATCH_LIMIT,),
        )
        rows = await cursor.fetchall()
    return [str(row['hour_start']) for row in rows]


async def run_card_enrichment_worker(
    stop_event: asyncio.Event | None = None,
) -> None:
    """Sleep loop that enriches recent un-enriched hourly cards.

    Args:
        stop_event: Optional external stop signal. When set the loop
            exits at the next iteration boundary. Defaults to a fresh
            event so the worker runs forever until cancelled.
    """
    stop = stop_event or asyncio.Event()
    log.info(
        'card_enrichment_worker.started',
        poll_s=POLL_INTERVAL_SECONDS,
        batch_limit=_BATCH_LIMIT,
    )

    while not stop.is_set():
        await beat(_WORKER_NAME)
        try:
            if not await _is_enabled():
                log.debug('card_enrichment_worker.disabled')
            else:
                pending = await _select_pending()
                if not pending:
                    log.debug('card_enrichment_worker.no_pending')
                else:
                    enriched = 0
                    for hour_start in pending:
                        result = await enrich_card(hour_start)
                        if result['status'] == 'ok':
                            enriched += 1
                        elif result['status'] == 'missing_config':
                            # No point hammering the rest of the batch
                            # if the provider isn't configured.
                            log.info(
                                'card_enrichment_worker.missing_config_break',
                            )
                            break
                    if enriched:
                        log.info(
                            'card_enrichment_worker.cycle',
                            enriched=enriched,
                            considered=len(pending),
                        )
        except asyncio.CancelledError:
            log.info('card_enrichment_worker.cancelled')
            raise
        except Exception as exc:
            log.exception(
                'card_enrichment_worker.iteration_failed',
                error=str(exc),
            )

        try:
            await asyncio.wait_for(
                stop.wait(),
                timeout=POLL_INTERVAL_SECONDS,
            )
        except TimeoutError:
            continue

    log.info('card_enrichment_worker.stopped')


__all__ = ['POLL_INTERVAL_SECONDS', 'run_card_enrichment_worker']
