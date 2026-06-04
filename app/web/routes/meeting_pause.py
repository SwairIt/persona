"""HTTP control surface for the smart-pause meeting detector (v1.19).

The capture loop hard-pauses when it sees a Zoom/Teams/Meet/etc.
window, but only when the user has opted in via the kv flag
``meeting_pause_enabled``. This module exposes that flag plus a
read-only view of the last detected meeting so the settings page can
render "you were in zoom.us at 14:02".

Endpoints
---------
- ``GET  /api/meeting-pause`` →
  ``{enabled: bool, last_meeting_app: str|None, last_meeting_at: str|None}``
- ``POST /api/meeting-pause`` body ``{enabled: bool}`` flips the flag.

The route file is intentionally self-contained: it does not register
itself in ``app/web/main.py``; the parent harness wires routers
elsewhere. Keeping the module tight makes it easy to drop into a
test client by importing ``router`` directly.
"""

from __future__ import annotations

from typing import TypedDict

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

router = APIRouter(tags=["meeting-pause"])
log = get_logger("persona.meeting_pause")

_KV_ENABLED: str = "meeting_pause_enabled"


class _TogglePayload(BaseModel):
    enabled: bool


class _LastMeeting(TypedDict):
    app: str | None
    at: str | None


async def _read_enabled() -> bool:
    async with get_connection() as conn:
        value = await get_kv(conn, _KV_ENABLED)
    return (value or "0").strip() == "1"


async def _read_last_meeting() -> _LastMeeting:
    """Return the most recent meeting_event's app + start time.

    Both fields are ``None`` when the table is empty (e.g. the user
    has never had the detector fire). We deliberately surface
    ``started_at`` rather than ``ended_at`` so the UI can render
    "you were in zoom.us starting at 14:02" even while the meeting
    is still in progress.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, started_at FROM meeting_event "
            "ORDER BY started_at DESC LIMIT 1",
        )
        row = await cursor.fetchone()
    if row is None:
        return {"app": None, "at": None}
    return {"app": str(row[0]), "at": str(row[1])}


@router.get("/api/meeting-pause")
async def meeting_pause_status() -> JSONResponse:
    """Return the current flag plus the last-detected-meeting summary."""
    enabled = await _read_enabled()
    last = await _read_last_meeting()
    return JSONResponse(
        {
            "enabled": enabled,
            "last_meeting_app": last["app"],
            "last_meeting_at": last["at"],
        }
    )


@router.post("/api/meeting-pause")
async def meeting_pause_toggle(payload: _TogglePayload) -> JSONResponse:
    """Flip the smart-pause flag. Takes effect on the next loop iteration."""
    new_value = "1" if payload.enabled else "0"
    async with get_connection() as conn:
        await set_kv(conn, _KV_ENABLED, new_value)
    log.info("meeting_pause.set", enabled=payload.enabled)
    return JSONResponse({"ok": True, "enabled": payload.enabled})
