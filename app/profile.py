"""T29 — per-user "about me" profile.

A free-text blob the user writes about themselves (who they are, what they
do, preferences, tone). Injected into the chat system prompt so the AI
actually knows the user instead of being blind. Stored in kv (single-user
deployment), keyed per user.
"""

from __future__ import annotations

from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

_MAX = 8000


def _key(user_id: int) -> str:
    return f"user_profile_{int(user_id)}"


async def get_profile(user_id: int) -> str:
    async with get_connection() as conn:
        return (await get_kv(conn, _key(user_id)) or "").strip()


async def set_profile(user_id: int, text: str) -> None:
    async with get_connection() as conn:
        await set_kv(conn, _key(user_id), (text or "").strip()[:_MAX])


def profile_block(text: str) -> str:
    """System-prompt fragment with the user's profile (empty if none)."""
    t = (text or "").strip()
    if not t:
        return ""
    return f"\n\n── О пользователе (помни это про него) ──\n{t}"
