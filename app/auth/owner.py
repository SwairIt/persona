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
# Доп. аккаунты с ПОЛНЫМ (owner-эквивалентным) доступом — kv full_access_user_ids
# (id через запятую). Для доверенных со-владельцев, которым нужно всё, а не только
# /chat+/billing. Кэш 60с, чтобы is_owner не бил в БД на каждый запрос гейта.
_fa_cache: dict[str, object] = {"value": None, "checked_at": 0.0}


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


async def _full_access_ids() -> set[int]:
    """Множество user_id с полным доступом (kv ``full_access_user_ids``). Кэш 60с."""
    now = time.monotonic()
    cached = _fa_cache["value"]
    if cached is not None and now - float(_fa_cache["checked_at"]) < _TTL:  # type: ignore[arg-type]
        return cached  # type: ignore[return-value]
    ids: set[int] = set()
    try:
        async with get_connection() as conn:
            raw = await get_kv(conn, "full_access_user_ids")
    except Exception:  # noqa: BLE001 — гейт не должен падать
        return cached if isinstance(cached, set) else set()
    for part in str(raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    _fa_cache["value"] = ids
    _fa_cache["checked_at"] = now
    return ids


async def is_owner(user_id: int | None) -> bool:
    if user_id is None:
        return False
    owner = await get_owner_user_id()
    if owner is not None and int(owner) == int(user_id):
        return True
    # Доп. со-владельцы с полным доступом (kv full_access_user_ids).
    return int(user_id) in await _full_access_ids()


async def is_primary_owner(user_id: int | None) -> bool:
    """True only for the single primary owner, never for full-access delegates."""
    if user_id is None:
        return False
    owner = await get_owner_user_id()
    return owner is not None and int(owner) == int(user_id)
