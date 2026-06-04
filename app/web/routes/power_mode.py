"""One-click "Power mode" toggle (v1.15).

Enables OCR + embeddings together — they are the two settings that
turn /ask from "useless on a fresh DB" into "actually finds things".
Defaults are off for privacy + CPU; this endpoint flips both at once
so the user doesn't have to walk two menus.

Bonus: also kicks the OCR worker into backfill for the last 7 days
of screenshots — without that the next /ask still sees empty ocr_text.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import set_kv

router = APIRouter(tags=["power-mode"])
log = get_logger("persona.power_mode")


class _PowerPayload(BaseModel):
    enabled: bool


@router.get("/api/power-mode")
async def power_mode_status() -> JSONResponse:
    """Report the current power-mode state (both flags AND together)."""
    from app.settings import get_settings  # noqa: PLC0415
    from app.storage.repository import get_kv  # noqa: PLC0415

    cfg = get_settings()
    async with get_connection() as conn:
        kv_ocr = await get_kv(conn, "ocr_enabled")
        kv_emb = await get_kv(conn, "embeddings_enabled")
    ocr_on = ((kv_ocr or ("1" if cfg.ocr_enabled else "0")).strip() == "1")
    emb_on = ((kv_emb or ("1" if cfg.embeddings_enabled else "0")).strip() == "1")
    return JSONResponse(
        {
            "enabled": ocr_on and emb_on,
            "ocr_enabled": ocr_on,
            "embeddings_enabled": emb_on,
        },
    )


@router.post("/api/power-mode")
async def power_mode_set(payload: _PowerPayload) -> JSONResponse:
    """Flip both OCR and embeddings flags atomically."""
    value = "1" if payload.enabled else "0"
    async with get_connection() as conn:
        await set_kv(conn, "ocr_enabled", value)
        await set_kv(conn, "embeddings_enabled", value)
    log.info("power_mode.set", enabled=payload.enabled)
    return JSONResponse(
        {
            "ok": True,
            "enabled": payload.enabled,
            "note": "Restart uvicorn for new captures; existing OCR backfills on next worker tick.",
        },
    )


__all__ = ["router"]
