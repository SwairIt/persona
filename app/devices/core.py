"""Device-row CRUD + heartbeat for the multi-device dashboard.

A device row is the binding between a user and a capture agent. The
``device_token`` is the secret the agent uses on every API call to prove
it belongs to user X — same model as a long-lived API token, but with a
per-device toggle the user can flip remotely from /devices.

The agent is not yet wired up to read these toggles; this module gives
the API + UI side. The local capture-loop will be taught to consult its
own ``device.capture_paused`` in a follow-up.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.devices")

_TOKEN_BYTES = 32

_VALID_KINDS = frozenset({"mac", "iphone", "windows", "web", "other"})


class DeviceRow(TypedDict):
    id: int
    user_id: int
    name: str
    kind: str
    device_token: str
    capture_paused: bool
    capture_interval_seconds: float | None
    created_at: str
    last_seen_at: str | None
    user_agent: str | None


def _coerce_kind(kind: str | None) -> str:
    """Map a raw kind string to one of ``_VALID_KINDS``."""
    if not kind:
        return "other"
    cleaned = kind.strip().lower()
    return cleaned if cleaned in _VALID_KINDS else "other"


def _row_to_device(row: object) -> DeviceRow:
    """Project a sqlite row mapping into our TypedDict."""
    return {
        "id": int(row["id"]),  # type: ignore[index]
        "user_id": int(row["user_id"]),  # type: ignore[index]
        "name": str(row["name"]),  # type: ignore[index]
        "kind": str(row["kind"]),  # type: ignore[index]
        "device_token": str(row["device_token"]),  # type: ignore[index]
        "capture_paused": bool(int(row["capture_paused"])),  # type: ignore[index]
        "capture_interval_seconds": (
            float(row["capture_interval_seconds"])  # type: ignore[index]
            if row["capture_interval_seconds"] is not None  # type: ignore[index]
            else None
        ),
        "created_at": str(row["created_at"]),  # type: ignore[index]
        "last_seen_at": (
            str(row["last_seen_at"]) if row["last_seen_at"] is not None else None  # type: ignore[index]
        ),
        "user_agent": (
            str(row["user_agent"]) if row["user_agent"] is not None else None  # type: ignore[index]
        ),
    }


async def register_device(
    user_id: int,
    name: str,
    kind: str = "other",
    user_agent: str | None = None,
) -> DeviceRow:
    """Insert a new device row and return it. Token is generated server-side."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("device name is required")
    if len(clean_name) > 64:
        clean_name = clean_name[:64]
    token = secrets.token_hex(_TOKEN_BYTES)
    clean_kind = _coerce_kind(kind)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "INSERT INTO device (user_id, name, kind, device_token, user_agent) "
            "VALUES (?, ?, ?, ?, ?)",
            (user_id, clean_name, clean_kind, token, user_agent),
        )
        await conn.commit()
        device_id = cursor.lastrowid or 0
        # Read back so we return the full row (with defaults filled in).
        cursor = await conn.execute(
            "SELECT * FROM device WHERE id = ?", (device_id,)
        )
        row = await cursor.fetchone()
    if row is None:
        raise RuntimeError("device insert reported success but lookup failed")
    log.info("device.registered", device_id=device_id, kind=clean_kind, user_id=user_id)
    return _row_to_device(row)


async def list_devices(user_id: int) -> list[DeviceRow]:
    """Return all devices belonging to ``user_id``, newest-seen first."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM device WHERE user_id = ? "
            "ORDER BY COALESCE(last_seen_at, created_at) DESC",
            (user_id,),
        )
        rows = await cursor.fetchall()
    return [_row_to_device(r) for r in rows]


async def get_device(user_id: int, device_id: int) -> DeviceRow | None:
    """Lookup a device by id, scoped to ``user_id`` so users can only see their own."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM device WHERE id = ? AND user_id = ?",
            (device_id, user_id),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_device(row)


async def lookup_by_token(token: str) -> DeviceRow | None:
    """Reverse lookup — used by the agent-facing API to authenticate calls."""
    if not token:
        return None
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT * FROM device WHERE device_token = ?", (token,)
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return _row_to_device(row)


async def heartbeat(token: str, user_agent: str | None = None) -> DeviceRow | None:
    """Agent calls this on each tick. Updates last_seen_at + optionally UA."""
    device = await lookup_by_token(token)
    if device is None:
        return None
    now = datetime.now(timezone.utc).isoformat()
    async with get_connection() as conn:
        if user_agent:
            await conn.execute(
                "UPDATE device SET last_seen_at = ?, user_agent = ? WHERE id = ?",
                (now, user_agent[:250], device["id"]),
            )
        else:
            await conn.execute(
                "UPDATE device SET last_seen_at = ? WHERE id = ?",
                (now, device["id"]),
            )
        await conn.commit()
    device["last_seen_at"] = now
    return device


async def set_capture_paused(
    user_id: int, device_id: int, paused: bool
) -> DeviceRow | None:
    """Flip the remote ``capture_paused`` toggle on a user's own device."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE device SET capture_paused = ? "
            "WHERE id = ? AND user_id = ?",
            (1 if paused else 0, device_id, user_id),
        )
        await conn.commit()
        if cursor.rowcount == 0:
            return None
    return await get_device(user_id, device_id)


async def set_capture_interval(
    user_id: int, device_id: int, seconds: float | None
) -> DeviceRow | None:
    """Override the per-device capture interval. ``None`` clears the override."""
    if seconds is not None:
        if seconds < 0.5 or seconds > 60.0:
            raise ValueError("capture interval must be between 0.5 and 60 seconds")
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE device SET capture_interval_seconds = ? "
            "WHERE id = ? AND user_id = ?",
            (seconds, device_id, user_id),
        )
        await conn.commit()
        if cursor.rowcount == 0:
            return None
    return await get_device(user_id, device_id)


async def rename_device(
    user_id: int, device_id: int, name: str
) -> DeviceRow | None:
    """Rename a device. The token / kind stay the same."""
    clean = (name or "").strip()
    if not clean:
        raise ValueError("name required")
    if len(clean) > 64:
        clean = clean[:64]
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE device SET name = ? WHERE id = ? AND user_id = ?",
            (clean, device_id, user_id),
        )
        await conn.commit()
        if cursor.rowcount == 0:
            return None
    return await get_device(user_id, device_id)


async def delete_device(user_id: int, device_id: int) -> bool:
    """Remove a device entirely. Future heartbeats with its token will 404."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "DELETE FROM device WHERE id = ? AND user_id = ?",
            (device_id, user_id),
        )
        await conn.commit()
        return cursor.rowcount > 0


async def rotate_token(user_id: int, device_id: int) -> DeviceRow | None:
    """Generate a fresh ``device_token``. Old one immediately invalid."""
    token = secrets.token_hex(_TOKEN_BYTES)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "UPDATE device SET device_token = ? "
            "WHERE id = ? AND user_id = ?",
            (token, device_id, user_id),
        )
        await conn.commit()
        if cursor.rowcount == 0:
            return None
    log.info("device.token_rotated", device_id=device_id, user_id=user_id)
    return await get_device(user_id, device_id)
