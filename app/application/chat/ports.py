"""Ports required by :class:`ConversationService`."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from app.application.chat.dto import (
        ConversationMessage,
        ModelRequest,
        ModelUsage,
        PreparedContext,
        ResolvedConversation,
        TurnCommand,
        TurnResult,
    )
    from app.domains.chat import ActorContext, ConversationId


class ConversationRepository(Protocol):
    async def get(
        self, actor: ActorContext, conversation_id: ConversationId
    ) -> ResolvedConversation | None: ...

    async def append_user(
        self, conversation_id: ConversationId, content: str
    ) -> ConversationMessage: ...

    async def history(
        self,
        conversation_id: ConversationId,
        *,
        max_turns: int,
        exclude_message_id: int,
    ) -> tuple[ConversationMessage, ...]: ...

    async def begin_assistant(
        self, conversation_id: ConversationId, *, provider: str | None
    ) -> int: ...

    async def update_assistant(self, message_id: int, content: str) -> None: ...

    async def finalize_assistant(
        self,
        message_id: int,
        content: str,
        *,
        elapsed_ms: int,
        usage: ModelUsage,
    ) -> None: ...

    async def append_system(
        self, conversation_id: ConversationId, content: str
    ) -> ConversationMessage: ...


class ConversationContextPort(Protocol):
    async def prepare(
        self,
        command: TurnCommand,
        conversation: ResolvedConversation,
        history: tuple[ConversationMessage, ...],
    ) -> PreparedContext: ...


class ModelStream(Protocol):
    @property
    def usage(self) -> ModelUsage: ...

    def deltas(self) -> AsyncIterator[str]: ...


class ConversationModelPort(Protocol):
    async def open_stream(self, request: ModelRequest) -> ModelStream: ...


class PostTurnPort(Protocol):
    async def dispatch(self, command: TurnCommand, result: TurnResult) -> None: ...


class MonotonicClock(Protocol):
    def now(self) -> float: ...
