"""Owner-gate: who is the primary account that can see the personal data.

The app's capture/memory data is currently global (not partitioned per
user). Full multi-tenant isolation would mean adding user_id to every
capture table and filtering ~300 routes — a huge, risky change. The safe
MVP isolation for a single-owner product is an **owner-gate**: exactly one
account (the owner) can access the private surface; any other authenticated
account is sandboxed to /pending and can NEVER see the owner's data.

Owner id resolution: kv ``owner_user_id`` if set, else the lowest user id.
Cached in-process (60s TTL) so the gate doesn't hit the DB every request.
"""

from __future__ import annotations

import time

from app.storage.db import get_connection
from app.storage.repository import get_kv

_TTL = 60.0
_cache: dict[str, float | int | None] = {"value": None, "checked_at": 0.0}


async def get_owner_user_id() -> int | None:
    now = time.monotonic()
    if _cache["value"] is not None and now - float(_cache["checked_at"]) < _TTL:
        return int(_cache["value"])  # type: ignore[arg-type]
    owner: int | None = None
    try:
        async with get_connection() as conn:
            raw = await get_kv(conn, "owner_user_id")
            if raw and str(raw).strip().isdigit():
                owner = int(str(raw).strip())
            else:
                cursor = await conn.execute("SELECT MIN(id) AS m FROM users")
                row = await cursor.fetchone()
                if row is not None and row["m"] is not None:
                    owner = int(row["m"])
    except Exception:  # noqa: BLE001 — never let the gate brick the app
        return _cache["value"]  # type: ignore[return-value]
    if owner is not None:
        _cache["value"] = owner
        _cache["checked_at"] = now
    return owner


async def is_owner(user_id: int | None) -> bool:
    if user_id is None:
        return False
    owner = await get_owner_user_id()
    return owner is not None and int(owner) == int(user_id)
