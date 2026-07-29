"""Telegram adapter for the channel-independent conversation application."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING

from app.adapters.conversation import build_conversation_service
from app.application.ambient_group import AmbientGroupService, AmbientGroupTurn
from app.application.chat import ConversationService, TurnCommand
from app.chat import append_message, create_session, get_session, touch_session
from app.domains.chat import (
    ActorContext,
    ConversationId,
    ConversationSurface,
    TenantId,
    UserId,
)
from app.integrations.telegram.ambient import (
    TelegramAmbientDecisionAdapter,
    TelegramAmbientTurnAdapter,
)
from app.integrations.telegram.output_guard import persona_only_reply

if TYPE_CHECKING:
    from app.integrations.telegram.repository import TelegramRepository


class PersonaTelegramService:
    """Map Telegram threads and delegate every LLM turn to ConversationService."""

    def __init__(
        self,
        repository: TelegramRepository,
        *,
        conversation_service: ConversationService | None = None,
        ambient_group_service: AmbientGroupService | None = None,
    ) -> None:
        self._repository = repository
        self._conversation = conversation_service or build_conversation_service()
        self._ambient = ambient_group_service or AmbientGroupService(
            TelegramAmbientDecisionAdapter(repository),
            TelegramAmbientTurnAdapter(repository),
        )
        self._mapping_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def respond(
        self,
        *,
        persona_user_id: int,
        telegram_chat_id: int,
        question: str,
        chat_title: str,
        image_data_url: str | None = None,
        sender_label: str | None = None,
        is_owner: bool | None = None,
        include_private_context: bool = True,
        allow_tools: bool = False,
        correlation_id: str = "",
        trusted_identity_context: str = "",
    ) -> str:
        clean = (question or "").strip()
        if not clean:
            raise ValueError("empty Telegram message")
        async with self._mapping_locks[int(telegram_chat_id)]:
            session_id = await self._get_or_create_session(
                persona_user_id, telegram_chat_id, chat_title
            )
        owner_actor = include_private_context if is_owner is None else is_owner
        group_turn = telegram_chat_id < 0 and bool(sender_label)
        turn_text = (
            f"[Telegram group · {sender_label}] {clean}"
            if group_turn
            else clean
        )
        result = await self._conversation.handle_turn(
            TurnCommand(
                actor=ActorContext(
                    tenant_id=TenantId(persona_user_id),
                    user_id=UserId(persona_user_id),
                    is_owner=owner_actor,
                ),
                surface=ConversationSurface.TELEGRAM,
                conversation_id=ConversationId(session_id),
                text=turn_text,
                image_data_url=image_data_url,
                source_label=(
                    None
                    if group_turn
                    else (f"Telegram · {sender_label}" if sender_label else "Telegram")
                ),
                include_private_context=include_private_context,
                allow_tools=allow_tools,
                max_tokens=64 if telegram_chat_id < 0 else 128,
                temperature=0.82,
                correlation_id=correlation_id,
                metadata={
                    "telegram_identity_context": trusted_identity_context[:600]
                },
            )
        )
        guarded = persona_only_reply(result.answer)
        return guarded or "Я не буду придумывать ответы за других участников."

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

    async def handle_ambient_group_message(
        self,
        *,
        persona_user_id: int,
        telegram_chat_id: int,
        update_id: int,
        message_id: int,
        text: str,
        chat_title: str,
        sender_label: str,
        image_data_url: str | None = None,
        reply_to_sender_label: str = "",
        reply_to_text: str = "",
        is_owner: bool = False,
        sender_telegram_user_id: int = 0,
        sender_username: str = "",
        sender_is_bot: bool = False,
        trusted_identity_context: str = "",
    ) -> str:
        """Persist one ordinary group message and optionally answer it."""
        clean = (text or "").strip()
        if not clean:
            return ""
        async with self._mapping_locks[int(telegram_chat_id)]:
            session_id = await self._get_or_create_session(
                persona_user_id,
                telegram_chat_id,
                chat_title,
            )
        outcome = await self._ambient.handle(
            AmbientGroupTurn(
                tenant_id=persona_user_id,
                conversation_id=session_id,
                external_chat_id=telegram_chat_id,
                update_id=update_id,
                message_id=message_id,
                text=clean,
                sender_label=sender_label,
                chat_title=chat_title,
                image_data_url=image_data_url,
                reply_to_sender_label=reply_to_sender_label,
                reply_to_text=reply_to_text,
                is_owner=is_owner,
                sender_telegram_user_id=sender_telegram_user_id,
                sender_username=sender_username,
                sender_is_bot=sender_is_bot,
                trusted_identity_context=trusted_identity_context[:16_000],
            )
        )
        return outcome.reply

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
