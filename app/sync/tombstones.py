"""Tombstone helpers for anti-resurrection during sync apply.

A tombstone says "this (kind, identity) was deleted at clock=N — refuse
to re-create it on later events with clock <= N". The reconcile handlers
consult :func:`is_tombstoned` before any INSERT/UPDATE, and call
:func:`stamp` whenever they apply a delete.

``identity`` is the entity's stable cross-device id:
  * note       → ``payload["uuid"]``
  * tag        → ``payload["uuid"]``
  * annotation → ``payload["shot_uuid"]`` (one annotation per shot, so
                   the shot's uuid is also the annotation's identity)
  * shot_tag   → composite ``"{shot_uuid}:{tag_name}"`` so deleting one
                   pair does NOT tombstone the entire shot or tag
  * kv         → ``payload["key"]``
"""

from __future__ import annotations

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.sync.tombstones")


def identity_for(kind: str, payload: dict[str, object]) -> str | None:
    """Pick the natural identity string for the (kind, payload) pair."""
    if kind in ("note", "tag"):
        uuid = str(payload.get("uuid") or "").strip()
        return uuid or None
    if kind == "annotation":
        shot_uuid = str(payload.get("shot_uuid") or "").strip()
        return shot_uuid or None
    if kind == "shot_tag":
        shot_uuid = str(payload.get("shot_uuid") or "").strip()
        tag_name = str(payload.get("tag_name") or "").strip()
        if not shot_uuid:
            return None
        return f"{shot_uuid}:{tag_name}" if tag_name else f"{shot_uuid}:*"
    if kind == "kv":
        key = str(payload.get("key") or "").strip()
        return key or None
    return None


async def is_tombstoned(kind: str, identity: str, incoming_clock: int) -> bool:
    """Return True when an insert/update with ``incoming_clock`` should
    be silently rejected because a later delete has been recorded."""
    if not kind or not identity:
        return False
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT clock FROM sync_tombstone WHERE kind = ? AND identity = ?",
            (kind, identity),
        )
        row = await cursor.fetchone()
    if row is None:
        return False
    tomb_clock = int(row["clock"])
    return incoming_clock <= tomb_clock


async def stamp(kind: str, identity: str, clock: int) -> None:
    """Record (or bump) the tombstone for (kind, identity)."""
    if not kind or not identity:
        return
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT clock FROM sync_tombstone WHERE kind = ? AND identity = ?",
            (kind, identity),
        )
        row = await cursor.fetchone()
        if row is None:
            await conn.execute(
                "INSERT INTO sync_tombstone (kind, identity, clock) "
                "VALUES (?, ?, ?)",
                (kind, identity, clock),
            )
        elif int(row["clock"]) < clock:
            await conn.execute(
                "UPDATE sync_tombstone "
                "SET clock = ?, deleted_at = datetime('now') "
                "WHERE kind = ? AND identity = ?",
                (clock, kind, identity),
            )
        await conn.commit()
    log.debug("sync.tombstone.stamped", kind=kind, identity=identity, clock=clock)
