"""Telegram adapter for the channel-independent conversation application."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING

from app.adapters.conversation import build_conversation_service
from app.application.chat import ConversationService, TurnCommand
from app.chat import append_message, create_session, get_session, touch_session
from app.domains.chat import (
    ActorContext,
    ConversationId,
    ConversationSurface,
    TenantId,
    UserId,
)

if TYPE_CHECKING:
    from app.integrations.telegram.repository import TelegramRepository


class PersonaTelegramService:
    """Map Telegram threads and delegate every LLM turn to ConversationService."""

    def __init__(
        self,
        repository: TelegramRepository,
        *,
        conversation_service: ConversationService | None = None,
    ) -> None:
        self._repository = repository
        self._conversation = conversation_service or build_conversation_service()
        self._mapping_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def respond(
        self,
        *,
        persona_user_id: int,
        telegram_chat_id: int,
        question: str,
        chat_title: str,
        sender_label: str | None = None,
        include_private_context: bool = True,
    ) -> str:
        clean = (question or "").strip()
        if not clean:
            raise ValueError("empty Telegram message")
        async with self._mapping_locks[int(telegram_chat_id)]:
            session_id = await self._get_or_create_session(
                persona_user_id, telegram_chat_id, chat_title
            )
        result = await self._conversation.handle_turn(
            TurnCommand(
                actor=ActorContext(
                    tenant_id=TenantId(persona_user_id),
                    user_id=UserId(persona_user_id),
                    is_owner=include_private_context,
                ),
                surface=ConversationSurface.TELEGRAM,
                conversation_id=ConversationId(session_id),
                text=clean,
                source_label=(
                    f"Telegram · {sender_label}" if sender_label else "Telegram"
                ),
                include_private_context=include_private_context,
                allow_tools=False,
                max_tokens=1800,
                temperature=0.65,
            )
        )
        return result.answer

    async def record_passive_group_message(
        self,
        *,
        persona_user_id: int,
        telegram_chat_id: int,
        text: str,
        chat_title: str,
        sender_label: str,
    ) -> None:
        """Persist allowlisted group context without invoking the model."""
        clean = (text or "").strip()
        if not clean:
            return
        async with self._mapping_locks[int(telegram_chat_id)]:
            session_id = await self._get_or_create_session(
                persona_user_id, telegram_chat_id, chat_title
            )
            await append_message(
                session_id, "user", f"[Telegram · {sender_label}] {clean}"
            )
            await touch_session(persona_user_id, session_id)

    async def reset_chat(self, telegram_chat_id: int) -> None:
        await self._repository.clear_session_id(telegram_chat_id)

    async def _get_or_create_session(
        self, persona_user_id: int, telegram_chat_id: int, chat_title: str
    ) -> int:
        existing_id = await self._repository.session_id(telegram_chat_id)
        if existing_id is not None:
            session = await get_session(persona_user_id, existing_id)
            if session is not None:
                return existing_id
        session = await create_session(
            persona_user_id, title=f"Telegram · {chat_title}"[:120]
        )
        session_id = int(session["id"])
        await self._repository.save_session_id(telegram_chat_id, session_id)
        return session_id


__all__ = ["PersonaTelegramService"]
