"""HTTP surface for the per-shot LLM alt-text feature (v1.32).

Two JSON endpoints:

* ``GET  /api/shots/alt-text`` — return the gate state. Used by the
  settings UI to render the toggle.
* ``POST /api/shots/alt-text`` — flip the kv gate. Body is JSON
  ``{"enabled": bool}``. Returns ``{"ok": true, "enabled": <bool>}``.

The gate kv row is shared with
:mod:`app.workers.alt_text_worker` — flipping it here takes effect on
the worker's next poll without a process restart.
"""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

router = APIRouter(tags=["shot-alt-text"])
log = get_logger("persona.shot_alt_text.web")

_KV_ENABLED: Final[str] = "shot_alt_text_enabled"
"""Gate kv row — must match
:data:`app.workers.alt_text_worker._KV_ENABLED`."""


async def _read_enabled() -> bool:
    """Return ``True`` iff the gate kv row holds the string ``"1"``."""
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_ENABLED)
    if raw is None:
        return False
    return raw.strip() == "1"


@router.get("/api/shots/alt-text", response_class=JSONResponse)
async def get_alt_text_settings() -> JSONResponse:
    """Return the current gate state for the per-shot alt-text feature."""
    enabled = await _read_enabled()
    log.info("shot_alt_text.web.get", enabled=enabled)
    return JSONResponse({"enabled": enabled})


@router.post("/api/shots/alt-text", response_class=JSONResponse)
async def set_alt_text_settings(request: Request) -> JSONResponse:
    """Flip the gate kv row. Body is JSON ``{"enabled": bool}``.

    Anything that isn't a JSON object with a boolean ``enabled`` key is
    rejected with HTTP 400 — we treat the API as strict input even
    though a misclick from the UI is the most common caller.
    """
    try:
        payload = await request.json()
    except Exception as exc:
        log.warning("shot_alt_text.web.bad_json", error=str(exc))
        raise HTTPException(status_code=400, detail="invalid_json") from exc

    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="expected_json_object")

    raw_enabled = payload.get("enabled")
    if not isinstance(raw_enabled, bool):
        raise HTTPException(status_code=400, detail="enabled_must_be_bool")

    async with get_connection() as conn:
        await set_kv(conn, _KV_ENABLED, "1" if raw_enabled else "0")

    log.info("shot_alt_text.web.set", enabled=raw_enabled)
    return JSONResponse({"ok": True, "enabled": raw_enabled})


__all__ = ["router"]
