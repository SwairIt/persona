"""Live microphone kill-switch (v1.14).

Lets the user pause the audio worker on demand — e.g. when watching a
film in headphones and the open mic ruins the audio. The flag lives in
``kv_settings.audio_capture_paused_live`` and is consulted by the audio
worker each loop iteration, so the change takes effect within
``POLL_INTERVAL_SECONDS`` (~5 s) without restarting the daemon.

Endpoints:
- ``GET  /api/audio/mic`` → ``{paused: bool}``
- ``POST /api/audio/mic`` body ``{paused: bool}`` → flip the flag

The HTML toolbar button at the top of the screen calls these via fetch.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.auth import current_user_optional
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.sync.kv_hook import maybe_emit_kv

router = APIRouter(tags=["mic-toggle"])
log = get_logger("persona.mic_toggle")

_KV_PAUSED: str = "audio_capture_paused_live"


class _TogglePayload(BaseModel):
    paused: bool


async def _read_flag() -> bool:
    async with get_connection() as conn:
        value = await get_kv(conn, _KV_PAUSED)
    return (value or "0").strip() == "1"


@router.get("/api/audio/mic")
async def mic_status() -> JSONResponse:
    """Return the current live-mic-pause flag."""
    return JSONResponse({"paused": await _read_flag()})


@router.post("/api/audio/mic")
async def mic_toggle(payload: _TogglePayload, request: Request) -> JSONResponse:
    """Flip the live mic pause flag.

    Takes effect within ~5 s. When the user is signed in, also fans the
    change out via a kv sync event so any other device of theirs picks
    it up on the next pull.
    """
    new_value = "1" if payload.paused else "0"
    async with get_connection() as conn:
        await set_kv(conn, _KV_PAUSED, new_value)
    log.info("mic_toggle.set", paused=payload.paused)

    # Best-effort multi-device fan-out. Failure here never breaks the
    # local toggle — see ``maybe_emit_kv`` for how it swallows errors.
    session = await current_user_optional(request)
    if session is not None:
        await maybe_emit_kv(
            key=_KV_PAUSED,
            value=new_value,
            user_id=session["user_id"],
        )

    return JSONResponse({"ok": True, "paused": payload.paused})
