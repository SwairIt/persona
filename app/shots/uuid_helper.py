"""Lazy uuid backfill + id/uuid lookup for screenshots.

The sync system addresses shots by uuid (so the same shot on two
devices has the same identifier). The legacy ``screenshots.id`` is kept
as the local primary key. ``ensure_uuid`` mints one if missing — it's
called from any sync-aware code path that needs to refer to a shot.
"""

from __future__ import annotations

import uuid as uuid_module

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.shots.uuid")


async def ensure_uuid(shot_id: int) -> str | None:
    """Return the shot's uuid, generating one if NULL. ``None`` if the
    shot doesn't exist."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT uuid FROM screenshots WHERE id = ?", (shot_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        existing = row["uuid"]
        if existing:
            return str(existing)
        new_uuid = str(uuid_module.uuid4())
        await conn.execute(
            "UPDATE screenshots SET uuid = ? WHERE id = ?",
            (new_uuid, shot_id),
        )
        await conn.commit()
    log.debug("shots.uuid.minted", shot_id=shot_id, uuid=new_uuid)
    return new_uuid


async def find_shot_id_by_uuid(shot_uuid: str) -> int | None:
    """Reverse lookup: uuid → numeric id. ``None`` when no row matches."""
    if not shot_uuid:
        return None
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id FROM screenshots WHERE uuid = ? LIMIT 1",
            (shot_uuid,),
        )
        row = await cursor.fetchone()
    return int(row["id"]) if row else None


async def find_shot_uuid_by_id(shot_id: int) -> str | None:
    """Forward lookup: id → uuid (does NOT auto-mint)."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT uuid FROM screenshots WHERE id = ? LIMIT 1",
            (shot_id,),
        )
        row = await cursor.fetchone()
    if row is None or row["uuid"] is None:
        return None
    return str(row["uuid"])
