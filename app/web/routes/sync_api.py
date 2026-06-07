"""Multi-device sync — pull/push API + admin status page.

Two surfaces:

  * Agent-facing JSON API authenticated by ``X-Device-Token`` header:
      - GET  /api/sync/pull?since=N&limit=500   → events the agent hasn't seen
      - POST /api/sync/push                     → append a batch of events
                                                     from the agent's local log
      - GET  /api/sync/state                    → current watermark

  * User-facing HTML at /sync showing event throughput + per-device
    watermarks so the owner can see if a device is falling behind.

The API uses the same device_token pattern as /api/devices/heartbeat
(T3) — no separate auth machinery.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.devices import lookup_by_token
from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.sync import (
    append_event,
    bump_pulled_watermark,
    bump_pushed_clock,
    get_state,
    list_events_since,
)
from app.web.templates_engine import templates

router = APIRouter(tags=["sync"])
log = get_logger("persona.sync.routes")

_PUSH_MAX_BATCH = 200


async def _auth_device(request: Request) -> dict[str, Any]:
    """Resolve X-Device-Token → device row, or raise 401."""
    token = request.headers.get("x-device-token", "")
    if not token:
        raise HTTPException(status_code=401, detail="missing X-Device-Token header")
    device = await lookup_by_token(token)
    if device is None:
        raise HTTPException(status_code=401, detail="unknown device token")
    return dict(device)


# --- Agent API -------------------------------------------------------------


@router.get("/api/sync/pull", response_class=JSONResponse)
async def sync_pull(
    request: Request,
    since: int = Query(default=0, ge=0),
    limit: int = Query(default=500, ge=1, le=2000),
) -> JSONResponse:
    """Return events the device hasn't seen yet."""
    device = await _auth_device(request)
    events = await list_events_since(device["user_id"], since_id=since, limit=limit)
    # Bump the device's pull watermark to the largest id we just shipped
    # so a follow-up pull with the same `since` is cheap.
    if events:
        await bump_pulled_watermark(device["id"], events[-1]["id"])
    return JSONResponse({"events": events, "count": len(events)})


@router.post("/api/sync/push", response_class=JSONResponse)
async def sync_push(
    request: Request,
    body: Annotated[dict[str, Any], Body(default_factory=dict)],
) -> JSONResponse:
    """Append a batch of agent-produced events.

    Body shape:
        {"events": [{kind, op, payload, entity_id?, logical_clock}, ...]}
    """
    device = await _auth_device(request)
    events_in = body.get("events", []) if isinstance(body, dict) else []
    if not isinstance(events_in, list):
        raise HTTPException(status_code=400, detail="events must be a list")
    if len(events_in) > _PUSH_MAX_BATCH:
        raise HTTPException(
            status_code=413, detail=f"batch must be <= {_PUSH_MAX_BATCH} events"
        )

    appended: list[int] = []
    skipped = 0
    state = await get_state(device["id"])
    high_clock = state["last_pushed_clock"]
    for entry in events_in:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        try:
            clock = int(entry.get("logical_clock", 0))
        except (TypeError, ValueError):
            skipped += 1
            continue
        # Dedup retried events using the per-device pushed-clock watermark.
        if clock and clock <= state["last_pushed_clock"]:
            skipped += 1
            continue
        try:
            new_id = await append_event(
                user_id=device["user_id"],
                kind=str(entry.get("kind", "")),
                op=str(entry.get("op", "")),
                payload=(
                    entry.get("payload")
                    if isinstance(entry.get("payload"), dict)
                    else {}
                ),
                entity_id=(
                    int(entry["entity_id"])
                    if entry.get("entity_id") is not None
                    else None
                ),
                device_id=device["id"],
                logical_clock=clock,
            )
        except ValueError as exc:
            log.warning("sync.push.rejected", error=str(exc))
            skipped += 1
            continue
        appended.append(new_id)
        if clock > high_clock:
            high_clock = clock
    if high_clock > state["last_pushed_clock"]:
        await bump_pushed_clock(device["id"], high_clock)
    return JSONResponse(
        {"appended": appended, "appended_count": len(appended), "skipped": skipped}
    )


@router.get("/api/sync/state", response_class=JSONResponse)
async def sync_state(request: Request) -> JSONResponse:
    """Current per-device watermark."""
    device = await _auth_device(request)
    state = await get_state(device["id"])
    return JSONResponse({"device_id": device["id"], **state})


# --- User-facing dashboard -------------------------------------------------


@router.get("/sync", response_class=HTMLResponse, response_model=None)
async def sync_dashboard(
    request: Request,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    """Show the sync log + per-device watermarks for the signed-in user."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n FROM sync_event WHERE user_id = ?",
            (session["user_id"],),
        )
        total_row = await cursor.fetchone()
        total = int(total_row["n"]) if total_row else 0

        cursor = await conn.execute(
            "SELECT id, kind, op, entity_id, logical_clock, server_recv_at, "
            "       device_id, applied_at "
            "FROM sync_event WHERE user_id = ? "
            "ORDER BY id DESC LIMIT 30",
            (session["user_id"],),
        )
        recent_rows = await cursor.fetchall()
        recent = [
            {
                "id": int(r["id"]),
                "kind": r["kind"],
                "op": r["op"],
                "entity_id": r["entity_id"],
                "logical_clock": int(r["logical_clock"]),
                "server_recv_at": r["server_recv_at"],
                "device_id": r["device_id"],
                "applied_at": r["applied_at"],
            }
            for r in recent_rows
        ]

        cursor = await conn.execute(
            "SELECT d.id, d.name, d.kind, "
            "       COALESCE(s.last_pulled_event_id, 0) AS last_pulled, "
            "       COALESCE(s.last_pushed_clock, 0)    AS last_pushed "
            "FROM device d "
            "LEFT JOIN device_sync_state s ON s.device_id = d.id "
            "WHERE d.user_id = ?",
            (session["user_id"],),
        )
        watermark_rows = await cursor.fetchall()
        watermarks = [
            {
                "id": int(r["id"]),
                "name": r["name"],
                "kind": r["kind"],
                "last_pulled": int(r["last_pulled"]),
                "last_pushed": int(r["last_pushed"]),
                "behind": max(0, total - int(r["last_pulled"])),
            }
            for r in watermark_rows
        ]

    return templates.TemplateResponse(
        request,
        "sync_dashboard.html",
        {
            "title": "Sync",
            "active_nav": "",
            "total_events": total,
            "recent": recent,
            "watermarks": watermarks,
        },
    )
