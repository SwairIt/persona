"""Settings endpoints for the daily-pin LLM enrichment toggle (v1.19).

The :mod:`app.workers.daily_pin_enrichment_worker` polls
``kv_settings.daily_pin_llm_enrichment_enabled`` every 30 minutes and
only spends tokens when the value is ``'1'``. These two endpoints are
the operator-facing way to read and flip that flag.

* ``GET  /api/daily-pin/enrichment`` → ``{ok, enabled}``
* ``POST /api/daily-pin/enrichment`` body ``{enabled: bool}`` →
  ``{ok, enabled}``

The flag is disabled by default (row absent ≡ ``'0'``). Flipping the
toggle does not retroactively enrich every old pin in one burst — the
worker picks the seven most recent un-enriched pins per cycle, so a
long backlog catches up over subsequent ticks.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

router = APIRouter(tags=["daily-pin-enrichment"])
log = get_logger("persona.daily_pin_enrichment_settings")

#: kv_settings row consulted by the worker. Must stay in sync with
#: :mod:`app.workers.daily_pin_enrichment_worker`.
_KV_FLAG: str = "daily_pin_llm_enrichment_enabled"


class _TogglePayload(BaseModel):
    """JSON body for the POST endpoint."""

    enabled: bool


async def _read_flag() -> bool:
    """Return ``True`` when the kv row is the literal string ``'1'``."""
    async with get_connection() as conn:
        value = await get_kv(conn, _KV_FLAG)
    return (value or "0").strip() == "1"


@router.get("/api/daily-pin/enrichment")
async def get_enrichment_state() -> JSONResponse:
    """Return the current daily-pin enrichment-enabled flag."""
    enabled = await _read_flag()
    return JSONResponse({"ok": True, "enabled": enabled})


@router.post("/api/daily-pin/enrichment")
async def set_enrichment_state(payload: _TogglePayload) -> JSONResponse:
    """Flip the daily-pin enrichment-enabled flag. Takes effect within ~30 min."""
    new_value = "1" if payload.enabled else "0"
    async with get_connection() as conn:
        await set_kv(conn, _KV_FLAG, new_value)
    log.info("daily_pin_enrichment_settings.set", enabled=payload.enabled)
    return JSONResponse({"ok": True, "enabled": payload.enabled})


__all__ = ["router"]
