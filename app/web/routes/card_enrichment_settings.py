"""Settings endpoints for the hourly-card LLM enrichment toggle (v1.14).

The :mod:`app.workers.card_enrichment_worker` polls
``kv_settings.hourly_card_llm_enrichment_enabled`` every 10 minutes
and only spends tokens when the value is ``'1'``. These two endpoints
are the operator-facing way to read and flip that flag.

* ``GET  /api/cards/enrichment`` → ``{ok, enabled}``
* ``POST /api/cards/enrichment`` body ``{enabled: bool}`` → ``{ok, enabled}``

The flag is disabled by default (row absent ≡ ``'0'``). Flipping the
toggle does not retroactively enrich older cards — the worker picks
the three most recent un-enriched cards per cycle, so a long backlog
catches up over subsequent ticks rather than in one burst.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

router = APIRouter(tags=['card-enrichment'])
log = get_logger('persona.card_enrichment_settings')

#: kv_settings row consulted by the worker. Must stay in sync with
#: :mod:`app.workers.card_enrichment_worker`.
_KV_FLAG: str = 'hourly_card_llm_enrichment_enabled'


class _TogglePayload(BaseModel):
    """JSON body for the POST endpoint."""

    enabled: bool


async def _read_flag() -> bool:
    """Return ``True`` when the kv row is the literal string ``'1'``."""
    async with get_connection() as conn:
        value = await get_kv(conn, _KV_FLAG)
    return (value or '0').strip() == '1'


@router.get('/api/cards/enrichment')
async def get_enrichment_state() -> JSONResponse:
    """Return the current enrichment-enabled flag."""
    enabled = await _read_flag()
    return JSONResponse({'ok': True, 'enabled': enabled})


@router.post('/api/cards/enrichment')
async def set_enrichment_state(payload: _TogglePayload) -> JSONResponse:
    """Flip the enrichment-enabled flag. Takes effect within ~10 min."""
    new_value = '1' if payload.enabled else '0'
    async with get_connection() as conn:
        await set_kv(conn, _KV_FLAG, new_value)
    log.info('card_enrichment_settings.set', enabled=payload.enabled)
    return JSONResponse({'ok': True, 'enabled': payload.enabled})


__all__ = ['router']
