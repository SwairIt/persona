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

from app.storage.db import get_connection, write_transaction
from app.storage.repository import delete_kv, get_kv, set_kv

_OWNER_TG_KEY = "telegram_owner_user_id"
_OWNER_PERSONA_KEY = "telegram_owner_persona_user_id"
_ALLOWED_CHATS_KEY = "telegram_allowed_chat_ids"
_PAIRING_HASH_KEY = "telegram_pairing_secret_hash"
_UPDATE_OFFSET_KEY = "telegram_update_offset"
_WORKER_LEASE_NAME = "telegram-update-consumer"
_MAX_LEASE_SECONDS = 600
_MAX_GROUP_BEHAVIOR_RULES = 16


@dataclass(frozen=True, slots=True)
class TelegramBinding:
    telegram_user_id: int
    persona_user_id: int


class TelegramRepository:
    async def acquire_worker_lease(
        self,
        holder_id: str,
        *,
        lease_seconds: int = _MAX_LEASE_SECONDS,
    ) -> bool:
        """Acquire or renew the singleton Telegram update-consumer lease."""
        holder = str(holder_id or "").strip()
        if not holder or len(holder) > 160:
            raise ValueError("invalid Telegram worker lease holder")
        modifier = _lease_modifier(lease_seconds)
        async with write_transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO runtime_lease
                    (name, holder_id, lease_until, updated_at)
                VALUES (?, ?, datetime('now', ?), datetime('now'))
                ON CONFLICT(name) DO UPDATE SET
                    holder_id=excluded.holder_id,
                    lease_until=excluded.lease_until,
                    updated_at=excluded.updated_at
                WHERE runtime_lease.holder_id=excluded.holder_id
                   OR runtime_lease.lease_until <= datetime('now')
                RETURNING holder_id
                """,
                (_WORKER_LEASE_NAME, holder, modifier),
            )
            row = await cursor.fetchone()
        return row is not None and str(row["holder_id"]) == holder

    async def renew_processing_lease(
        self,
        update_id: int,
        holder_id: str,
        *,
        lease_seconds: int = _MAX_LEASE_SECONDS,
    ) -> bool:
        """Atomically renew the singleton worker and one in-flight update.

        Renewal is allowed only while both leases are still live. A suspended
        process cannot wake up and revive a lease that another process could
        already have acquired.
        """
        holder = _holder(holder_id)
        update = _update_id(update_id)
        modifier = _lease_modifier(lease_seconds)
        async with write_transaction() as conn:
            active = await conn.execute(
                """
                SELECT 1
                  FROM runtime_lease AS worker
                  JOIN telegram_update_inbox AS inbox
                    ON inbox.update_id=?
                 WHERE worker.name=?
                   AND worker.holder_id=?
                   AND worker.lease_until>datetime('now')
                   AND inbox.status='processing'
                   AND inbox.holder_id=?
                   AND inbox.lease_until>datetime('now')
                """,
                (update, _WORKER_LEASE_NAME, holder, holder),
            )
            if await active.fetchone() is None:
                return False
            worker = await conn.execute(
                """
                UPDATE runtime_lease
                   SET lease_until=datetime('now', ?),
                       updated_at=datetime('now')
                 WHERE name=?
                   AND holder_id=?
                """,
                (modifier, _WORKER_LEASE_NAME, holder),
            )
            if worker.rowcount != 1:
                return False
            inbox = await conn.execute(
                """
                UPDATE telegram_update_inbox
                   SET lease_until=datetime('now', ?),
                       updated_at=datetime('now')
                 WHERE update_id=?
                   AND status='processing'
                   AND holder_id=?
                """,
                (modifier, update, holder),
            )
            return inbox.rowcount == 1

    async def release_worker_lease(self, holder_id: str) -> None:
        holder = str(holder_id or "").strip()
        if not holder:
            return
        async with write_transaction() as conn:
            await conn.execute(
                "DELETE FROM runtime_lease WHERE name=? AND holder_id=?",
                (_WORKER_LEASE_NAME, holder),
            )

    async def worker_lease_holder(self) -> str | None:
        """Return the exact singleton holder for safe orphan detection."""

        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT holder_id FROM runtime_lease WHERE name=?",
                (_WORKER_LEASE_NAME,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        holder = str(row["holder_id"] or "").strip()
        return holder or None

    async def claim_update(
        self,
        update_id: int,
        holder_id: str,
        *,
        lease_seconds: int = _MAX_LEASE_SECONDS,
    ) -> bool:
        """Reserve a never-before-seen update while holding the live worker lease.

        Any existing row, including one left ``processing`` by a crashed
        process, suppresses replay. This intentionally favours at-most-once
        DB/LLM handling over retrying an update whose side effects are unknown.
        """
        update = _update_id(update_id)
        holder = _holder(holder_id)
        modifier = _lease_modifier(lease_seconds)
        async with write_transaction() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO telegram_update_inbox(
                    update_id, status, holder_id, lease_until,
                    first_seen_at, updated_at
                )
                SELECT ?, 'processing', ?, datetime('now', ?),
                       datetime('now'), datetime('now')
                 WHERE EXISTS (
                    SELECT 1
                      FROM runtime_lease
                     WHERE name=?
                       AND holder_id=?
                       AND lease_until>datetime('now')
                 )
                ON CONFLICT(update_id) DO NOTHING
                RETURNING update_id
                """,
                (
                    update,
                    holder,
                    modifier,
                    _WORKER_LEASE_NAME,
                    holder,
                ),
            )
            return await cursor.fetchone() is not None

    async def finish_update(
        self,
        update_id: int,
        holder_id: str,
        *,
        status: str,
        outcome: str,
    ) -> bool:
        """Finish an inbox row only while this process still owns both leases."""
        if status not in {"processed", "failed"}:
            raise ValueError("invalid Telegram inbox terminal status")
        update = _update_id(update_id)
        holder = _holder(holder_id)
        safe_outcome = "".join(
            char for char in str(outcome or "") if char.isalnum() or char in "._-"
        )[:80]
        async with write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE telegram_update_inbox
                   SET status=?,
                       holder_id=NULL,
                       lease_until=NULL,
                       outcome=?,
                       processed_at=datetime('now'),
                       updated_at=datetime('now')
                 WHERE update_id=?
                   AND status='processing'
                   AND holder_id=?
                   AND lease_until>datetime('now')
                   AND EXISTS (
                       SELECT 1
                         FROM runtime_lease
                        WHERE name=?
                          AND holder_id=?
                          AND lease_until>datetime('now')
                   )
                """,
                (
                    status,
                    safe_outcome or status,
                    update,
                    holder,
                    _WORKER_LEASE_NAME,
                    holder,
                ),
            )
            return cursor.rowcount == 1

    async def save_update_offset_if_leased(
        self,
        offset: int,
        holder_id: str,
    ) -> bool:
        """Advance the poll cursor only for the current singleton consumer."""
        holder = _holder(holder_id)
        safe_offset = max(0, int(offset))
        async with write_transaction() as conn:
            lease = await conn.execute(
                """
                SELECT 1
                  FROM runtime_lease
                 WHERE name=?
                   AND holder_id=?
                   AND lease_until>datetime('now')
                """,
                (_WORKER_LEASE_NAME, holder),
            )
            if await lease.fetchone() is None:
                return False
            await conn.execute(
                """
                INSERT INTO kv_settings(key, value, updated_at)
                VALUES(?, ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                (_UPDATE_OFFSET_KEY, str(safe_offset)),
            )
            return True

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

    async def group_behavior_rules(self, chat_id: int) -> tuple[str, ...]:
        """Return owner-authored, group-local behavior rules, oldest first."""
        async with get_connection() as conn:
            raw = await get_kv(conn, _group_behavior_key(chat_id))
        try:
            values = json.loads(raw or "[]")
        except (TypeError, ValueError):
            return ()
        if not isinstance(values, list):
            return ()
        return tuple(
            str(value).strip()[:500]
            for value in values[-_MAX_GROUP_BEHAVIOR_RULES:]
            if isinstance(value, str) and value.strip()
        )

    async def remember_group_behavior_rule(self, chat_id: int, rule: str) -> None:
        """Persist one trusted owner rule; newer rules take precedence."""
        clean = " ".join(str(rule or "").split())[:500]
        if int(chat_id) >= 0 or not clean:
            raise ValueError("group behavior rule requires a group chat and text")
        values = list(await self.group_behavior_rules(chat_id))
        values = [value for value in values if value.casefold() != clean.casefold()]
        values.append(clean)
        async with get_connection() as conn:
            await set_kv(
                conn,
                _group_behavior_key(chat_id),
                json.dumps(values[-_MAX_GROUP_BEHAVIOR_RULES:], ensure_ascii=False),
            )

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

    async def save_last_bot_message(self, chat_id: int, message_id: int) -> None:
        chat = int(chat_id)
        message = int(message_id)
        if chat == 0 or message <= 0:
            raise ValueError("invalid Telegram bot message identity")
        async with get_connection() as conn:
            await set_kv(conn, _last_bot_message_key(chat), str(message))

    async def last_bot_message_id(self, chat_id: int) -> int | None:
        async with get_connection() as conn:
            raw = await get_kv(conn, _last_bot_message_key(int(chat_id)))
        try:
            value = int(raw) if raw is not None else 0
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    async def clear_last_bot_message(self, chat_id: int) -> None:
        async with get_connection() as conn:
            await delete_kv(conn, _last_bot_message_key(int(chat_id)))


def _session_key(chat_id: int) -> str:
    return f"telegram_chat_session_{int(chat_id)}"


def _last_bot_message_key(chat_id: int) -> str:
    if int(chat_id) == 0:
        raise ValueError("invalid Telegram chat id")
    return f"telegram_last_bot_message:{int(chat_id)}"


def _group_behavior_key(chat_id: int) -> str:
    return f"telegram_group_behavior:{int(chat_id)}"


def _holder(value: str) -> str:
    holder = str(value or "").strip()
    if not holder or len(holder) > 160:
        raise ValueError("invalid Telegram worker lease holder")
    return holder


def _update_id(value: int) -> int:
    update = int(value)
    if update < 0:
        raise ValueError("Telegram update_id cannot be negative")
    return update


def _lease_modifier(value: int) -> str:
    return f"+{max(30, min(int(value), _MAX_LEASE_SECONDS))} seconds"


__all__ = ["TelegramBinding", "TelegramRepository"]
