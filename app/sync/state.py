"""Per-device sync watermarks: which event ids each device has pulled,
which logical clocks it has pushed.

The state row is created lazily on the first call. Devices can call
``get_state`` to discover their own watermark when they reconnect.
"""

from __future__ import annotations

from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.sync.state")


class SyncState(TypedDict):
    device_id: int
    last_pulled_event_id: int
    last_pushed_clock: int
    updated_at: str


async def _ensure_row(device_id: int) -> None:
    async with get_connection() as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO device_sync_state (device_id) VALUES (?)",
            (device_id,),
        )
        await conn.commit()


async def get_state(device_id: int) -> SyncState:
    """Return the current watermark row, creating it if missing."""
    await _ensure_row(device_id)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT device_id, last_pulled_event_id, last_pushed_clock, updated_at "
            "FROM device_sync_state WHERE device_id = ?",
            (device_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        # Should be unreachable after _ensure_row, but stay defensive.
        return {
            "device_id": device_id,
            "last_pulled_event_id": 0,
            "last_pushed_clock": 0,
            "updated_at": "",
        }
    return {
        "device_id": int(row["device_id"]),
        "last_pulled_event_id": int(row["last_pulled_event_id"]),
        "last_pushed_clock": int(row["last_pushed_clock"]),
        "updated_at": str(row["updated_at"]),
    }


async def bump_pulled_watermark(device_id: int, new_high: int) -> None:
    """Advance ``last_pulled_event_id`` if the caller pulled past the previous one."""
    if new_high <= 0:
        return
    await _ensure_row(device_id)
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE device_sync_state "
            "SET last_pulled_event_id = ?, updated_at = datetime('now') "
            "WHERE device_id = ? AND last_pulled_event_id < ?",
            (new_high, device_id, new_high),
        )
        await conn.commit()


async def bump_pushed_clock(device_id: int, clock: int) -> None:
    """Advance the legacy global ``last_pushed_clock``.

    Retained for back-compat with the T4 substrate. New code paths should
    use :func:`bump_pushed_clock_for_kind` so each event kind gets its own
    Lamport counter — a global counter incorrectly rejected legitimate
    pushes of one kind after another kind had advanced the counter.
    """
    if clock <= 0:
        return
    await _ensure_row(device_id)
    async with get_connection() as conn:
        await conn.execute(
            "UPDATE device_sync_state "
            "SET last_pushed_clock = ?, updated_at = datetime('now') "
            "WHERE device_id = ? AND last_pushed_clock < ?",
            (clock, device_id, clock),
        )
        await conn.commit()


async def get_pushed_clock_for_kind(device_id: int, kind: str) -> int:
    """Return the high-water mark this device has pushed for ``kind``."""
    if not kind:
        return 0
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT high_clock FROM device_push_clock "
            "WHERE device_id = ? AND kind = ?",
            (device_id, kind),
        )
        row = await cursor.fetchone()
    return int(row["high_clock"]) if row else 0


async def bump_pushed_clock_for_kind(
    device_id: int, kind: str, clock: int
) -> None:
    """Advance per-(device, kind) push clock. Monotonic upsert."""
    if not kind or clock <= 0:
        return
    async with get_connection() as conn:
        # Manual UPSERT — the partial unique idx_tags_uuid pattern from
        # migration 154 reminded us that SQLite ON CONFLICT can be finicky
        # with composite keys + ``IF NOT EXISTS`` table semantics. Plain
        # SELECT-then-INSERT/UPDATE is robust.
        cursor = await conn.execute(
            "SELECT high_clock FROM device_push_clock "
            "WHERE device_id = ? AND kind = ?",
            (device_id, kind),
        )
        row = await cursor.fetchone()
        if row is None:
            await conn.execute(
                "INSERT INTO device_push_clock (device_id, kind, high_clock) "
                "VALUES (?, ?, ?)",
                (device_id, kind, clock),
            )
        elif int(row["high_clock"]) < clock:
            await conn.execute(
                "UPDATE device_push_clock SET high_clock = ?, updated_at = datetime('now') "
                "WHERE device_id = ? AND kind = ?",
                (clock, device_id, kind),
            )
        await conn.commit()
