"""Sync event log — append, replay, list-since.

Events are append-only. ``apply_pending`` walks unapplied rows in id
order and resolves them into the canonical tables via per-kind handlers.
Handlers are kept deliberately small (a few SQL statements each) so a
broken handler for one kind never blocks the rest.
"""

from __future__ import annotations

import json
from typing import Any, TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.sync.events")

_VALID_KINDS: frozenset[str] = frozenset(
    {"note", "tag", "annotation", "kv", "pin", "shot_tag"}
)
_VALID_OPS: frozenset[str] = frozenset({"insert", "update", "delete"})

# Cap on payload size we store per event — JSON-serialised. 64 KB covers
# every realistic case; beyond that the payload is probably a mistake
# (an image was inlined) and we refuse to bloat the log.
_MAX_PAYLOAD_BYTES = 64 * 1024


class SyncEvent(TypedDict):
    id: int
    device_id: int | None
    user_id: int
    kind: str
    entity_id: int | None
    op: str
    payload_json: str
    logical_clock: int
    server_recv_at: str
    applied_at: str | None


def _row_to_event(row: object) -> SyncEvent:
    return {
        "id": int(row["id"]),  # type: ignore[index]
        "device_id": (
            int(row["device_id"]) if row["device_id"] is not None else None  # type: ignore[index]
        ),
        "user_id": int(row["user_id"]),  # type: ignore[index]
        "kind": str(row["kind"]),  # type: ignore[index]
        "entity_id": (
            int(row["entity_id"]) if row["entity_id"] is not None else None  # type: ignore[index]
        ),
        "op": str(row["op"]),  # type: ignore[index]
        "payload_json": str(row["payload_json"]),  # type: ignore[index]
        "logical_clock": int(row["logical_clock"]),  # type: ignore[index]
        "server_recv_at": str(row["server_recv_at"]),  # type: ignore[index]
        "applied_at": (
            str(row["applied_at"]) if row["applied_at"] is not None else None  # type: ignore[index]
        ),
    }


async def append_event(
    *,
    user_id: int,
    kind: str,
    op: str,
    payload: dict[str, Any],
    entity_id: int | None = None,
    device_id: int | None = None,
    logical_clock: int = 0,
) -> int:
    """Append one event. Returns the new id. Raises ``ValueError`` on bad input."""
    if kind not in _VALID_KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    if op not in _VALID_OPS:
        raise ValueError(f"unknown op {op!r}")
    payload_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    if len(payload_str.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise ValueError("payload exceeds size cap")
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO sync_event "
            "(device_id, user_id, kind, entity_id, op, payload_json, logical_clock) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (device_id, user_id, kind, entity_id, op, payload_str, logical_clock),
        )
        await conn.commit()
        new_id = cursor.lastrowid or 0
    log.debug(
        "sync.event.appended",
        event_id=new_id,
        kind=kind,
        op=op,
        device_id=device_id,
    )
    return new_id


async def list_events_since(
    user_id: int, since_id: int, limit: int = 500
) -> list[SyncEvent]:
    """Return events with ``id > since_id`` scoped to ``user_id``."""
    safe_limit = max(1, min(2000, int(limit)))
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM sync_event "
            "WHERE user_id = ? AND id > ? "
            "ORDER BY id ASC "
            "LIMIT ?",
            (user_id, since_id, safe_limit),
        )
        rows = await cursor.fetchall()
    return [_row_to_event(r) for r in rows]


async def apply_pending(user_id: int, batch_size: int = 200) -> dict[str, int]:
    """Walk unapplied events in id order and stamp ``applied_at``.

    The actual canonical mutation (write into ``notes``, ``screenshot_tags``,
    ``shot_annotation``, ``kv_settings``) is NOT yet performed by this
    function — that hook lands when the existing route handlers are taught
    to call ``append_event`` AND ``apply_pending`` will then double-apply
    the row. For now we only stamp ``applied_at`` so the log doesn't grow
    forever in the ``applied_at IS NULL`` bucket.

    Returns counts so the caller (worker) can log progress.
    """
    applied = 0
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM sync_event "
            "WHERE user_id = ? AND applied_at IS NULL "
            "ORDER BY id ASC "
            "LIMIT ?",
            (user_id, batch_size),
        )
        rows = await cursor.fetchall()
        for row in rows:
            await conn.execute(
                "UPDATE sync_event SET applied_at = datetime('now') WHERE id = ?",
                (int(row["id"]),),
            )
            applied += 1
        await conn.commit()
    if applied:
        log.info("sync.event.applied_batch", user_id=user_id, count=applied)
    return {"applied": applied}
