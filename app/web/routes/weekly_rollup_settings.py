"""Settings endpoints for the weekly LLM rollup toggle (v1.27).

The :mod:`app.workers.weekly_rollup_worker` polls
``kv_settings.weekly_llm_rollup_enabled`` every hour and only spends
tokens when the value is ``'1'``. These two endpoints are the
operator-facing way to read and flip that flag.

* ``GET  /api/weekly-rollup`` → ``{ok, enabled}``
* ``POST /api/weekly-rollup`` body ``{enabled: bool}`` → ``{ok, enabled}``

The flag is disabled by default (row absent ≡ ``'0'``). Flipping the
toggle does not retroactively rebuild older rollups beyond the
worker's four-week lookback window — older weeks stay heuristic-only.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

router = APIRouter(tags=["weekly-rollup"])
log = get_logger("persona.weekly_rollup_settings")

#: kv_settings row consulted by the worker. Must stay in sync with
#: :mod:`app.workers.weekly_rollup_worker`.
_KV_FLAG: str = "weekly_llm_rollup_enabled"


class _TogglePayload(BaseModel):
    """JSON body for the POST endpoint."""

    enabled: bool


async def _read_flag() -> bool:
    """Return ``True`` only when the kv row is the literal string ``'1'``."""
    async with get_connection() as conn:
        value = await get_kv(conn, _KV_FLAG)
    return (value or "0").strip() == "1"


@router.get("/api/weekly-rollup")
async def get_weekly_rollup_state() -> JSONResponse:
    """Return the current weekly-rollup-enabled flag."""
    enabled = await _read_flag()
    return JSONResponse({"ok": True, "enabled": enabled})


@router.post("/api/weekly-rollup")
async def set_weekly_rollup_state(payload: _TogglePayload) -> JSONResponse:
    """Flip the weekly-rollup-enabled flag. Takes effect within ~1h."""
    new_value = "1" if payload.enabled else "0"
    async with get_connection() as conn:
        await set_kv(conn, _KV_FLAG, new_value)
    log.info("weekly_rollup_settings.set", enabled=payload.enabled)
    return JSONResponse({"ok": True, "enabled": payload.enabled})


__all__ = ["router"]
