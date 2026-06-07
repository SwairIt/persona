"""User-facing device dashboard.

Two surfaces:

  * /devices              — list of the signed-in user's devices,
                            register form, per-device pause toggle,
                            interval override, rename and remove buttons.
  * /devices/{id}/token   — shows the device_token once after registration
                            or rotation so the user can paste it into the
                            agent config. The token is otherwise displayed
                            only as a fingerprint (first 8 + last 4 chars).

Plus an agent-facing JSON endpoint:

  * POST /api/devices/heartbeat — the capture agent calls this on each
                                  tick with its device_token; we update
                                  ``last_seen_at`` and return the current
                                  remote-control state (paused, interval).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.devices import (
    delete_device,
    get_device,
    heartbeat,
    list_devices,
    register_device,
    rename_device,
    rotate_token,
    set_capture_interval,
    set_capture_paused,
)
from app.logging_setup import get_logger
from app.web.templates_engine import templates

router = APIRouter(tags=["devices"])
log = get_logger("persona.devices.routes")


def _token_fingerprint(token: str) -> str:
    """Return ``aaaaaaaa…bbbb`` so the table can show a stable hint
    without ever leaking the secret."""
    if len(token) < 14:
        return token
    return f"{token[:8]}…{token[-4:]}"


@router.get("/devices", response_class=HTMLResponse, response_model=None)
async def devices_list(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    """Render the list of devices for the signed-in user."""
    devices = await list_devices(session["user_id"])
    fp_map = {d["id"]: _token_fingerprint(d["device_token"]) for d in devices}
    return templates.TemplateResponse(
        request,
        "devices.html",
        {
            "title": "Твои устройства",
            "active_nav": "",
            "devices": devices,
            "fingerprints": fp_map,
            "session": session,
            "just_registered_token": request.query_params.get("token"),
        },
    )


@router.post("/devices/new", response_model=None)
async def devices_create(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    name: Annotated[str, Form()],
    kind: Annotated[str, Form()] = "other",
) -> RedirectResponse:
    """Create a new device row and redirect to /devices with the token
    pre-shown so the user can copy it into the agent config."""
    try:
        device = await register_device(
            user_id=session["user_id"],
            name=name,
            kind=kind,
            user_agent=request.headers.get("user-agent"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # PRG with the brand-new token surfaced ONCE through a query param.
    return RedirectResponse(
        url=f"/devices?token={device['device_token']}", status_code=303
    )


@router.post("/devices/{device_id}/pause", response_model=None)
async def devices_pause_toggle(
    request: Request,
    device_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    paused: Annotated[str, Form()] = "1",
) -> RedirectResponse:
    """Flip the remote-control ``capture_paused`` flag."""
    wanted = paused not in ("0", "false", "")
    device = await set_capture_paused(session["user_id"], device_id, wanted)
    if device is None:
        raise HTTPException(status_code=404)
    return RedirectResponse(url="/devices", status_code=303)


@router.post("/devices/{device_id}/interval", response_model=None)
async def devices_interval_set(
    request: Request,
    device_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    seconds: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Set or clear the per-device interval override."""
    parsed: float | None
    if seconds.strip() == "":
        parsed = None
    else:
        try:
            parsed = float(seconds)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="bad interval") from exc
    try:
        device = await set_capture_interval(session["user_id"], device_id, parsed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if device is None:
        raise HTTPException(status_code=404)
    return RedirectResponse(url="/devices", status_code=303)


@router.post("/devices/{device_id}/rename", response_model=None)
async def devices_rename(
    request: Request,
    device_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    name: Annotated[str, Form()],
) -> RedirectResponse:
    """Rename a device. Token stays."""
    try:
        device = await rename_device(session["user_id"], device_id, name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if device is None:
        raise HTTPException(status_code=404)
    return RedirectResponse(url="/devices", status_code=303)


@router.post("/devices/{device_id}/rotate-token", response_model=None)
async def devices_rotate(
    request: Request,
    device_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    """Generate a fresh device_token. Old token invalidated immediately."""
    device = await rotate_token(session["user_id"], device_id)
    if device is None:
        raise HTTPException(status_code=404)
    return RedirectResponse(
        url=f"/devices?token={device['device_token']}", status_code=303
    )


@router.post("/devices/{device_id}/delete", response_model=None)
async def devices_delete(
    request: Request,
    device_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    """Remove a device row entirely."""
    deleted = await delete_device(session["user_id"], device_id)
    if not deleted:
        raise HTTPException(status_code=404)
    return RedirectResponse(url="/devices", status_code=303)


# --- Agent-facing API ------------------------------------------------------


@router.post("/api/devices/heartbeat", response_class=JSONResponse)
async def device_heartbeat(request: Request) -> JSONResponse:
    """Agent-facing heartbeat. Authenticates by ``device_token`` header.

    The body is unused — heartbeat is a side-effect call. Response carries
    the current remote-control state so the agent can apply it locally
    (pause capture, switch interval).
    """
    token = request.headers.get("x-device-token", "")
    if not token:
        raise HTTPException(status_code=401, detail="missing X-Device-Token header")
    device = await heartbeat(token, user_agent=request.headers.get("user-agent"))
    if device is None:
        raise HTTPException(status_code=401, detail="unknown device token")
    return JSONResponse(
        {
            "device_id": device["id"],
            "capture_paused": device["capture_paused"],
            "capture_interval_seconds": device["capture_interval_seconds"],
            "last_seen_at": device["last_seen_at"],
        }
    )


# Convenience JSON to power future widgets without re-rendering the page.
@router.get("/api/devices.json", response_class=JSONResponse)
async def devices_json(
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> JSONResponse:
    devices = await list_devices(session["user_id"])
    # Strip the token from the JSON projection — the listing UI uses the
    # fingerprint, never the raw secret.
    projected = [
        {
            "id": d["id"],
            "name": d["name"],
            "kind": d["kind"],
            "capture_paused": d["capture_paused"],
            "capture_interval_seconds": d["capture_interval_seconds"],
            "last_seen_at": d["last_seen_at"],
            "created_at": d["created_at"],
        }
        for d in devices
    ]
    return JSONResponse({"devices": projected})
