"""Apply pending sync events into the canonical tables.

This is the half that migration 154 unlocked: events of kind ``note`` /
``kv`` produced by other devices get materialised into ``notes`` /
``kv_settings`` so a pull on device B observes mutations made on device A.

Conflict resolution is last-write-wins on ``logical_clock``. For ``kv``,
the resolution is per-key (the column ``kv_settings.last_applied_clock``
remembers the highest clock already seen). For ``note``, the resolution
is per-uuid (we always overwrite — the note has only one body field, no
sub-fields to merge).

Per-kind handler contract:
    Returns ``True`` when the event was applied (apply_pending will stamp
    ``applied_at`` on the row). Returns ``False`` when the event was
    deliberately skipped (e.g. stale clock) — still mark applied to take
    it off the pending queue. The third option is to raise — apply_pending
    catches and logs, leaving ``applied_at`` NULL so a future tick can
    retry.
"""

from __future__ import annotations

import json
from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.sync.reconcile")


def _decode_payload(raw: str) -> dict[str, Any]:
    try:
        decoded = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


async def _apply_note_event(event: dict[str, Any]) -> bool:
    """Materialise a ``note`` event into the ``notes`` table by uuid."""
    payload = _decode_payload(event["payload_json"])
    uuid = str(payload.get("uuid") or "").strip()
    if not uuid:
        log.warning("sync.apply.note.no_uuid", event_id=event["id"])
        return False
    op = event["op"]
    async with get_connection() as conn:
        if op == "delete":
            await conn.execute(
                "UPDATE notes SET deleted_at = datetime('now') "
                "WHERE uuid = ? AND deleted_at IS NULL",
                (uuid,),
            )
            await conn.commit()
            return True

        title = payload.get("title")
        body = str(payload.get("body") or "")
        source = payload.get("source") or "sync"
        # The unique index on notes.uuid is PARTIAL (WHERE uuid IS NOT NULL)
        # so SQLite refuses ON CONFLICT(uuid) — we do the upsert manually
        # with a SELECT, an UPDATE-or-INSERT, no race because the worker
        # is single-threaded inside one process.
        cursor = await conn.execute(
            "SELECT id FROM notes WHERE uuid = ? LIMIT 1",
            (uuid,),
        )
        existing = await cursor.fetchone()
        if existing is None:
            await conn.execute(
                """
                INSERT INTO notes (uuid, title, body, source, created_at, updated_at, encrypted)
                VALUES (?, ?, ?, ?, datetime('now'), datetime('now'), 0)
                """,
                (uuid, title, body, source),
            )
        else:
            await conn.execute(
                """
                UPDATE notes
                   SET title = ?, body = ?, source = ?,
                       updated_at = datetime('now'),
                       deleted_at = NULL
                 WHERE uuid = ?
                """,
                (title, body, source, uuid),
            )
        await conn.commit()
    return True


async def _apply_kv_event(event: dict[str, Any]) -> bool:
    """Materialise a ``kv`` event into ``kv_settings`` with clock check."""
    payload = _decode_payload(event["payload_json"])
    key = str(payload.get("key") or "").strip()
    if not key:
        log.warning("sync.apply.kv.no_key", event_id=event["id"])
        return False
    incoming_clock = int(event.get("logical_clock") or 0)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT last_applied_clock FROM kv_settings WHERE key = ?",
            (key,),
        )
        row = await cursor.fetchone()
        current_clock = int(row["last_applied_clock"]) if row else 0
        if incoming_clock and incoming_clock <= current_clock:
            # Stale — silently accept the event off the pending queue
            # without overwriting the newer canonical value.
            return False

        if event["op"] == "delete":
            await conn.execute(
                "DELETE FROM kv_settings WHERE key = ?", (key,)
            )
            await conn.commit()
            return True

        value = payload.get("value")
        if value is None:
            return False
        cursor = await conn.execute(
            "SELECT 1 FROM kv_settings WHERE key = ? LIMIT 1", (key,)
        )
        existing = await cursor.fetchone()
        if existing is None:
            await conn.execute(
                """
                INSERT INTO kv_settings (key, value, updated_at, last_applied_clock)
                VALUES (?, ?, datetime('now'), ?)
                """,
                (key, str(value), incoming_clock),
            )
        else:
            await conn.execute(
                """
                UPDATE kv_settings
                   SET value = ?, updated_at = datetime('now'),
                       last_applied_clock = ?
                 WHERE key = ?
                """,
                (str(value), incoming_clock, key),
            )
        await conn.commit()
    return True


_HANDLERS = {
    "note": _apply_note_event,
    "kv": _apply_kv_event,
}


async def apply_pending(user_id: int, batch_size: int = 200) -> dict[str, int]:
    """Walk unapplied events and materialise them into canonical tables.

    Returns ``{"applied", "skipped", "failed"}`` counts so the worker can
    log and so tests can assert.
    """
    applied = 0
    skipped = 0
    failed = 0
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, device_id, user_id, kind, entity_id, op,
                   payload_json, logical_clock, server_recv_at, applied_at
              FROM sync_event
             WHERE user_id = ? AND applied_at IS NULL
             ORDER BY id ASC
             LIMIT ?
            """,
            (user_id, batch_size),
        )
        rows = await cursor.fetchall()

    for row in rows:
        event = {
            "id": int(row["id"]),
            "device_id": row["device_id"],
            "user_id": int(row["user_id"]),
            "kind": str(row["kind"]),
            "entity_id": row["entity_id"],
            "op": str(row["op"]),
            "payload_json": str(row["payload_json"]),
            "logical_clock": int(row["logical_clock"]),
        }
        handler = _HANDLERS.get(event["kind"])
        if handler is None:
            # Unknown kind — mark applied so we don't loop forever, but
            # log a warning so we notice if a new kind ships without a
            # handler.
            log.warning("sync.apply.unknown_kind", kind=event["kind"], event_id=event["id"])
            async with get_connection() as conn:
                await conn.execute(
                    "UPDATE sync_event SET applied_at = datetime('now') WHERE id = ?",
                    (event["id"],),
                )
                await conn.commit()
            skipped += 1
            continue

        try:
            ok = await handler(event)
        except Exception as exc:
            log.warning(
                "sync.apply.handler_failed",
                kind=event["kind"],
                event_id=event["id"],
                error=str(exc),
            )
            failed += 1
            continue

        async with get_connection() as conn:
            await conn.execute(
                "UPDATE sync_event SET applied_at = datetime('now') WHERE id = ?",
                (event["id"],),
            )
            await conn.commit()
        if ok:
            applied += 1
        else:
            skipped += 1

    if applied or skipped or failed:
        log.info(
            "sync.apply.batch_done",
            user_id=user_id,
            applied=applied,
            skipped=skipped,
            failed=failed,
        )
    return {"applied": applied, "skipped": skipped, "failed": failed}
