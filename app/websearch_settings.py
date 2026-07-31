"""Persistence for the Brave Search opt-in key (2026-07-31).

Thin service layer so ``app/web/routes/settings_web_search.py`` doesn't hold
a direct ``app.storage.db`` import (the architecture gate in
``tests/test_architecture_gates.py`` forbids new routes from doing that —
new DB access must sit behind a small port/adapter like this one, mirroring
``app/thinking/settings.py``).

Same kv row :func:`app.mcp.builtin_tools._brave_key` already reads.
"""

from __future__ import annotations

from typing import Final

from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

#: kv_settings row. Kept in sync with the alternate name ``brave_api_key``
#: that ``web_search`` also accepts, but this is the only one this app writes.
KV_BRAVE_KEY: Final[str] = "byo_api_key_brave"


async def has_brave_key() -> bool:
    """True if a Brave Search API key is on file."""
    async with get_connection() as conn:
        value = await get_kv(conn, KV_BRAVE_KEY)
    return bool(value)


async def save_brave_key(api_key: str) -> None:
    """Persist a newly-pasted Brave key. Caller must pre-validate non-empty."""
    async with get_connection() as conn:
        await set_kv(conn, KV_BRAVE_KEY, api_key)


async def clear_brave_key() -> None:
    """Wipe the stored Brave key (falls back to the keyless provider)."""
    async with get_connection() as conn:
        await set_kv(conn, KV_BRAVE_KEY, "")
