"""Shared owner-exclusive mode lookup for auth routes and middleware."""

from __future__ import annotations

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv

log = get_logger("persona.auth.exclusive")

_KEY = "owner_exclusive_mode"


async def read_owner_exclusive_mode() -> bool:
    """Read the flag from KV.

    This low-level function intentionally lets database errors propagate so
    callers can choose an explicit fail-closed policy.
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KEY)
    return str(raw or "").strip() == "1"


async def owner_exclusive_enabled() -> bool:
    """Return the mode, denying enrollment/auth expansion on lookup failure."""
    try:
        return await read_owner_exclusive_mode()
    except Exception as exc:
        log.warning("owner_exclusive.lookup_failed_closed", error=str(exc))
        return True


__all__ = ["owner_exclusive_enabled", "read_owner_exclusive_mode"]
