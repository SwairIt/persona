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
from app.sync.tombstones import identity_for, is_tombstoned, stamp

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
    clock = int(event.get("logical_clock") or 0)

    # Anti-resurrection: any insert/update with clock <= tombstone clock
    # for this uuid is silently skipped (event still consumed off the queue).
    if op != "delete" and await is_tombstoned("note", uuid, clock):
        log.info("sync.apply.note.tombstoned", uuid=uuid, event_id=event["id"])
        return False

    async with get_connection() as conn:
        if op == "delete":
            await conn.execute(
                "UPDATE notes SET deleted_at = datetime('now') "
                "WHERE uuid = ? AND deleted_at IS NULL",
                (uuid,),
            )
            await conn.commit()
            await stamp("note", uuid, clock)
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
    op = event["op"]
    if op != "delete" and await is_tombstoned("kv", key, incoming_clock):
        log.info("sync.apply.kv.tombstoned", key=key, event_id=event["id"])
        return False
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

        if op == "delete":
            await conn.execute(
                "DELETE FROM kv_settings WHERE key = ?", (key,)
            )
            await conn.commit()
            await stamp("kv", key, incoming_clock)
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


async def _apply_tag_event(event: dict[str, Any]) -> bool:
    """Materialise a ``tag`` dictionary event into the ``tags`` table.

    Identity is ``uuid``. A rename event therefore finds the row by uuid
    and updates the ``name`` column — losing ``color`` is not acceptable.
    Cross-device tag attachments to specific screenshots will land in a
    later tick once ``screenshots.uuid`` exists; this handler only
    syncs the tag *dictionary* (name + color).
    """
    payload = _decode_payload(event["payload_json"])
    uuid = str(payload.get("uuid") or "").strip()
    if not uuid:
        log.warning("sync.apply.tag.no_uuid", event_id=event["id"])
        return False
    op = event["op"]
    clock = int(event.get("logical_clock") or 0)
    if op != "delete" and await is_tombstoned("tag", uuid, clock):
        log.info("sync.apply.tag.tombstoned", uuid=uuid, event_id=event["id"])
        return False
    async with get_connection() as conn:
        if op == "delete":
            await conn.execute("DELETE FROM tags WHERE uuid = ?", (uuid,))
            await conn.commit()
            await stamp("tag", uuid, clock)
            return True

        name = str(payload.get("name") or "").strip()
        if not name:
            log.warning("sync.apply.tag.no_name", event_id=event["id"])
            return False
        color = payload.get("color")
        cursor = await conn.execute(
            "SELECT id FROM tags WHERE uuid = ? LIMIT 1", (uuid,)
        )
        existing = await cursor.fetchone()
        if existing is None:
            # Insert can still collide on the legacy UNIQUE(name) index
            # if a local-only tag with the same name exists. In that case
            # adopt the local row by stamping the incoming uuid onto it.
            cursor = await conn.execute(
                "SELECT id FROM tags WHERE name = ? LIMIT 1", (name,)
            )
            name_clash = await cursor.fetchone()
            if name_clash is not None:
                await conn.execute(
                    "UPDATE tags SET uuid = ?, color = COALESCE(?, color) "
                    "WHERE id = ?",
                    (uuid, color, int(name_clash["id"])),
                )
            else:
                await conn.execute(
                    "INSERT INTO tags (uuid, name, color, created_at) "
                    "VALUES (?, ?, ?, datetime('now'))",
                    (uuid, name, color),
                )
        else:
            await conn.execute(
                "UPDATE tags SET name = ?, color = COALESCE(?, color) "
                "WHERE uuid = ?",
                (name, color, uuid),
            )
        await conn.commit()
    return True


async def _apply_annotation_event(event: dict[str, Any]) -> bool:
    """Materialise a ``annotation`` event into ``shot_annotation``.

    Identity is the pair (shot_uuid, annotation seq). For now the seq is
    implicit "latest wins" — we keep at most ONE annotation row per
    shot_uuid which gets overwritten on update events. Multi-revision
    history already lives in v1.45 ``shot_annotation_revision``; this
    handler only syncs the current head SVG payload.
    """
    payload = _decode_payload(event["payload_json"])
    shot_uuid = str(payload.get("shot_uuid") or "").strip()
    if not shot_uuid:
        log.warning("sync.apply.annotation.no_shot_uuid", event_id=event["id"])
        return False
    op = event["op"]
    clock = int(event.get("logical_clock") or 0)
    if op != "delete" and await is_tombstoned("annotation", shot_uuid, clock):
        log.info("sync.apply.annotation.tombstoned", shot_uuid=shot_uuid, event_id=event["id"])
        return False

    from app.shots import find_shot_id_by_uuid  # noqa: PLC0415 — break import cycle
    shot_id = await find_shot_id_by_uuid(shot_uuid)
    if shot_id is None:
        # The corresponding shot hasn't arrived on this device yet.
        # Returning False without raising skips applied_at stamp so the
        # event sits in the queue and gets retried — when ingest later
        # writes the screenshot with a matching uuid the next worker
        # tick picks it up.
        log.info(
            "sync.apply.annotation.shot_not_local",
            shot_uuid=shot_uuid,
            event_id=event["id"],
        )
        raise RuntimeError("shot not present locally — retry later")

    async with get_connection() as conn:
        if op == "delete":
            await conn.execute(
                "DELETE FROM shot_annotation WHERE screenshot_id = ?",
                (shot_id,),
            )
            await conn.commit()
            await stamp("annotation", shot_uuid, clock)
            return True

        svg = str(payload.get("svg_payload") or "")
        cursor = await conn.execute(
            "SELECT id FROM shot_annotation WHERE screenshot_id = ? LIMIT 1",
            (shot_id,),
        )
        existing = await cursor.fetchone()
        if existing is None:
            await conn.execute(
                """
                INSERT INTO shot_annotation (screenshot_id, svg_payload, shot_uuid,
                                             created_at, updated_at)
                VALUES (?, ?, ?, datetime('now'), datetime('now'))
                """,
                (shot_id, svg, shot_uuid),
            )
        else:
            await conn.execute(
                """
                UPDATE shot_annotation
                   SET svg_payload = ?, shot_uuid = ?,
                       updated_at = datetime('now')
                 WHERE screenshot_id = ?
                """,
                (svg, shot_uuid, shot_id),
            )
        await conn.commit()
    return True


async def _apply_shot_tag_event(event: dict[str, Any]) -> bool:
    """Materialise a per-shot tag attachment.

    Payload shape: ``{"shot_uuid": ..., "tag_uuid": ...}``. The handler
    resolves both uuids locally; tags are matched by uuid first, then
    falling back to name when only ``tag_name`` is in the payload (older
    clients that don't yet send tag_uuid).
    """
    payload = _decode_payload(event["payload_json"])
    shot_uuid = str(payload.get("shot_uuid") or "").strip()
    tag_uuid = str(payload.get("tag_uuid") or "").strip()
    tag_name = str(payload.get("tag_name") or "").strip()
    if not shot_uuid or (not tag_uuid and not tag_name):
        log.warning("sync.apply.shot_tag.bad_payload", event_id=event["id"])
        return False
    op = event["op"]
    clock = int(event.get("logical_clock") or 0)
    # shot_tag identity is composite; tombstone applies per-pair, so
    # deleting one tag attachment never blocks other attachments to the
    # same shot.
    identity = f"{shot_uuid}:{tag_name}" if tag_name else f"{shot_uuid}:*"
    if op != "delete" and await is_tombstoned("shot_tag", identity, clock):
        log.info("sync.apply.shot_tag.tombstoned", identity=identity, event_id=event["id"])
        return False

    from app.shots import find_shot_id_by_uuid  # noqa: PLC0415

    shot_id = await find_shot_id_by_uuid(shot_uuid)
    if shot_id is None:
        raise RuntimeError("shot not present locally — retry later")

    async with get_connection() as conn:
        tag_id: int | None = None
        if tag_uuid:
            cursor = await conn.execute(
                "SELECT id FROM tags WHERE uuid = ? LIMIT 1", (tag_uuid,)
            )
            row = await cursor.fetchone()
            if row is not None:
                tag_id = int(row["id"])
        if tag_id is None and tag_name:
            cursor = await conn.execute(
                "SELECT id FROM tags WHERE name = ? LIMIT 1", (tag_name,)
            )
            row = await cursor.fetchone()
            if row is None:
                # Create the tag on the fly so the attachment lands.
                cursor = await conn.execute(
                    "INSERT INTO tags (uuid, name, created_at) "
                    "VALUES (?, ?, datetime('now'))",
                    (tag_uuid or None, tag_name),
                )
                tag_id = cursor.lastrowid
            else:
                tag_id = int(row["id"])
        if tag_id is None:
            log.warning("sync.apply.shot_tag.unresolved", event_id=event["id"])
            return False

        if op == "delete":
            await conn.execute(
                "DELETE FROM screenshot_tags WHERE screenshot_id = ? AND tag_id = ?",
                (shot_id, tag_id),
            )
            await conn.commit()
            await stamp("shot_tag", identity, clock)
            return True

        # INSERT OR IGNORE — junction-table dedup. The pair is the natural
        # key; the legacy schema doesn't carry a unique constraint but
        # at the row level "attach this tag once" is what we want.
        await conn.execute(
            """
            INSERT OR IGNORE INTO screenshot_tags
                (screenshot_id, tag_id, shot_uuid, created_at)
            VALUES (?, ?, ?, datetime('now'))
            """,
            (shot_id, tag_id, shot_uuid),
        )
        await conn.commit()
    return True


_HANDLERS = {
    "note": _apply_note_event,
    "kv": _apply_kv_event,
    "tag": _apply_tag_event,
    "annotation": _apply_annotation_event,
    "shot_tag": _apply_shot_tag_event,
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
