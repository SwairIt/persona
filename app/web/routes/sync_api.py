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
    bump_pushed_clock_for_kind,
    get_pushed_clock_for_kind,
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
    """Return events the device hasn't seen yet.

    T14 (2026-06-07): events are filtered by the device's sync policy.
    A device that opted out of e.g. ``shot_blob`` events will never
    receive them in the pull stream, regardless of the ``since`` cursor.
    We still advance the watermark to the largest ID we considered
    (filtered-out included) so subsequent pulls don't re-scan them.
    """
    from app.devices import allowed_kinds_for_device  # noqa: PLC0415 - lazy import to avoid circular at module load

    device = await _auth_device(request)
    events = await list_events_since(device["user_id"], since_id=since, limit=limit)

    allowed = await allowed_kinds_for_device(device["id"])
    # Drop kinds the user told this device to mute. If every kind is
    # enabled (default), the comprehension is a no-op cost (one set
    # membership check per event).
    largest_id_considered = events[-1]["id"] if events else 0
    filtered = [e for e in events if e["kind"] in allowed]
    if largest_id_considered:
        await bump_pulled_watermark(device["id"], largest_id_considered)
    return JSONResponse({"events": filtered, "count": len(filtered)})


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
    # Per-kind clock cache so we read the watermark from DB at most once
    # per distinct kind in this batch, not once per event.
    per_kind_high: dict[str, int] = {}
    for entry in events_in:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        kind = str(entry.get("kind", "")).strip()
        try:
            clock = int(entry.get("logical_clock", 0))
        except (TypeError, ValueError):
            skipped += 1
            continue
        # Per-kind dedup. The kind-specific high-water mark lets a kv
        # push with clock=5 succeed even though a note push reached
        # clock=100 earlier — they're independent Lamport timelines.
        if clock and kind:
            if kind not in per_kind_high:
                per_kind_high[kind] = await get_pushed_clock_for_kind(
                    device["id"], kind
                )
            if clock <= per_kind_high[kind]:
                skipped += 1
                continue
        try:
            new_id = await append_event(
                user_id=device["user_id"],
                kind=kind,
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
        if kind and clock > per_kind_high.get(kind, 0):
            per_kind_high[kind] = clock
    # Persist updated high-water marks per kind.
    for kind, clock in per_kind_high.items():
        if clock > 0:
            await bump_pushed_clock_for_kind(device["id"], kind, clock)
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

        # Per-kind breakdown — useful as a smoke check that note / kv /
        # tag handlers are all firing.
        cursor = await conn.execute(
            "SELECT kind, COUNT(*) AS n, "
            "       SUM(CASE WHEN applied_at IS NULL THEN 1 ELSE 0 END) AS pending "
            "FROM sync_event WHERE user_id = ? "
            "GROUP BY kind ORDER BY n DESC",
            (session["user_id"],),
        )
        by_kind = [
            {
                "kind": str(r["kind"]),
                "total": int(r["n"]),
                "pending": int(r["pending"]),
            }
            for r in await cursor.fetchall()
        ]

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
            "title": "Синхронизация",
            "active_nav": "",
            "total_events": total,
            "recent": recent,
            "watermarks": watermarks,
            "by_kind": by_kind,
        },
    )
