"""Persistence boundary for Telegram access and chat mapping.

The integration stores a handful of control values in ``kv_settings``.  No
Telegram credential is persisted: only owner/chat ids, a one-time pairing
hash, update offset and the existing Persona chat session ids.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass

from app.storage.db import get_connection
from app.storage.repository import delete_kv, get_kv, set_kv

_OWNER_TG_KEY = "telegram_owner_user_id"
_OWNER_PERSONA_KEY = "telegram_owner_persona_user_id"
_ALLOWED_CHATS_KEY = "telegram_allowed_chat_ids"
_PAIRING_HASH_KEY = "telegram_pairing_secret_hash"
_UPDATE_OFFSET_KEY = "telegram_update_offset"


@dataclass(frozen=True, slots=True)
class TelegramBinding:
    telegram_user_id: int
    persona_user_id: int


class TelegramRepository:
    async def get_binding(self) -> TelegramBinding | None:
        async with get_connection() as conn:
            tg_raw = await get_kv(conn, _OWNER_TG_KEY)
            persona_raw = await get_kv(conn, _OWNER_PERSONA_KEY)
        try:
            if tg_raw is None or persona_raw is None:
                return None
            return TelegramBinding(int(tg_raw), int(persona_raw))
        except ValueError:
            return None

    async def bind_owner(self, telegram_user_id: int, persona_user_id: int) -> TelegramBinding:
        async with get_connection() as conn:
            await set_kv(conn, _OWNER_TG_KEY, str(int(telegram_user_id)))
            await set_kv(conn, _OWNER_PERSONA_KEY, str(int(persona_user_id)))
            await delete_kv(conn, _PAIRING_HASH_KEY)
        return TelegramBinding(int(telegram_user_id), int(persona_user_id))

    async def allowed_chat_ids(self) -> set[int]:
        async with get_connection() as conn:
            raw = await get_kv(conn, _ALLOWED_CHATS_KEY)
        try:
            values = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return set()
        return {
            int(value)
            for value in values
            if isinstance(value, int) or (isinstance(value, str) and value.lstrip("-").isdigit())
        }

    async def set_chat_allowed(self, chat_id: int, allowed: bool) -> None:
        values = await self.allowed_chat_ids()
        if allowed:
            values.add(int(chat_id))
        else:
            values.discard(int(chat_id))
        async with get_connection() as conn:
            await set_kv(conn, _ALLOWED_CHATS_KEY, json.dumps(sorted(values)))

    async def create_pairing_code(self) -> str:
        code = secrets.token_urlsafe(24)
        digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
        async with get_connection() as conn:
            await set_kv(conn, _PAIRING_HASH_KEY, digest)
        return code

    async def verify_pairing_code(self, candidate: str, configured_secret: str = "") -> bool:
        candidate_hash = hashlib.sha256((candidate or "").encode("utf-8")).hexdigest()
        if configured_secret:
            expected = hashlib.sha256(configured_secret.encode("utf-8")).hexdigest()
        else:
            async with get_connection() as conn:
                expected = (await get_kv(conn, _PAIRING_HASH_KEY) or "").strip()
        return bool(expected) and hmac.compare_digest(candidate_hash, expected)

    async def update_offset(self) -> int:
        async with get_connection() as conn:
            raw = await get_kv(conn, _UPDATE_OFFSET_KEY)
        try:
            return max(0, int(raw or 0))
        except ValueError:
            return 0

    async def save_update_offset(self, offset: int) -> None:
        async with get_connection() as conn:
            await set_kv(conn, _UPDATE_OFFSET_KEY, str(max(0, int(offset))))

    async def session_id(self, chat_id: int) -> int | None:
        async with get_connection() as conn:
            raw = await get_kv(conn, _session_key(chat_id))
        try:
            return int(raw) if raw is not None else None
        except ValueError:
            return None

    async def save_session_id(self, chat_id: int, session_id: int) -> None:
        async with get_connection() as conn:
            await set_kv(conn, _session_key(chat_id), str(int(session_id)))

    async def clear_session_id(self, chat_id: int) -> None:
        async with get_connection() as conn:
            await delete_kv(conn, _session_key(chat_id))


def _session_key(chat_id: int) -> str:
    return f"telegram_chat_session_{int(chat_id)}"


__all__ = ["TelegramBinding", "TelegramRepository"]
