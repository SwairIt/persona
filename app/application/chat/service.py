"""Channel-independent orchestration for a Persona conversation turn."""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from app.application.chat.dto import (
    ModelRequest,
    ModelUsage,
    TurnCommand,
    TurnEvent,
    TurnResult,
)
from app.domains.chat import (
    ConversationAccessDenied,
    ConversationNotFound,
    InvalidTurn,
    ModelUnavailable,
    TurnGenerationFailed,
    TurnState,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from app.application.chat.dto import (
        ConversationMessage,
        PreparedContext,
        ResolvedConversation,
    )
    from app.application.chat.ports import (
        ConversationContextPort,
        ConversationModelPort,
        ConversationRepository,
        ModelStream,
        MonotonicClock,
        PostTurnPort,
    )
    from app.domains.chat import ConversationId

_EMPTY_ANSWER = "(пустой ответ от модели)"


@dataclass(frozen=True, slots=True)
class _SystemClock:
    def now(self) -> float:
        return time.perf_counter()


@dataclass(slots=True)
class _GenerationState:
    started: float
    last_flush: float
    assistant_id: int | None = None
    chunks: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "".join(self.chunks)


class ConversationService:
    """Persist, contextualise, generate and finish a turn for every channel."""

    def __init__(
        self,
        repository: ConversationRepository,
        context: ConversationContextPort,
        model: ConversationModelPort,
        post_turn: PostTurnPort,
        *,
        clock: MonotonicClock | None = None,
        partial_flush_seconds: float = 1.0,
    ) -> None:
        self._repository = repository
        self._context = context
        self._model = model
        self._post_turn = post_turn
        self._clock = clock or _SystemClock()
        self._partial_flush_seconds = max(0.0, partial_flush_seconds)
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def handle_turn(self, command: TurnCommand) -> TurnResult:
        """Run a turn to completion and return its transport-neutral result."""
        failure = ""
        failure_type = ""
        async for event in self.stream_turn(command):
            if event.state is TurnState.COMPLETED and event.result is not None:
                return event.result
            if event.state is TurnState.FAILED:
                failure = event.detail
                failure_type = str(event.metadata.get("error_type") or "")
        if failure_type == "model_unavailable":
            raise ModelUnavailable(failure or "model unavailable")
        raise TurnGenerationFailed(failure or "conversation did not complete")

    async def stream_turn(self, command: TurnCommand) -> AsyncIterator[TurnEvent]:
        """Yield domain events while running one serialized conversation turn."""
        if not command.text.strip() and not command.image_data_url:
            raise InvalidTurn("message or image is required")
        async with self._locks[int(command.conversation_id)]:
            async for event in self._run_locked(command):
                yield event

    async def _run_locked(self, command: TurnCommand) -> AsyncIterator[TurnEvent]:
        conversation, user_message, history = await self._accept(command)
        yield TurnEvent(TurnState.ACCEPTED, metadata={"message_id": user_message.id})

        try:
            prepared = await self._context.prepare(command, conversation, history)
        except Exception as exc:
            async for event in self._context_failure(conversation.id, exc):
                yield event
            return
        yield TurnEvent(TurnState.CONTEXT_READY)

        try:
            stream = await self._open_stream(command, conversation, prepared)
        except ModelUnavailable as exc:
            await self._repository.append_system(conversation.id, str(exc))
            yield TurnEvent(
                TurnState.FAILED,
                detail=str(exc),
                metadata={"error_type": "model_unavailable"},
            )
            return

        async for event in self._generate(
            command, conversation, user_message, stream
        ):
            yield event

    async def _accept(
        self, command: TurnCommand
    ) -> tuple[
        ResolvedConversation,
        ConversationMessage,
        tuple[ConversationMessage, ...],
    ]:
        conversation = await self._repository.get(command.actor, command.conversation_id)
        if conversation is None:
            raise ConversationNotFound("conversation not found")
        if (
            conversation.tenant_id != int(command.actor.tenant_id)
            or conversation.id != command.conversation_id
        ):
            raise ConversationAccessDenied("conversation tenant mismatch")
        user_text = command.model_text or "Опиши прикреплённое изображение."
        user_message = await self._repository.append_user(conversation.id, user_text)
        history = await self._repository.history(
            conversation.id,
            max_turns=20,
            exclude_message_id=user_message.id,
        )
        return conversation, user_message, history

    async def _context_failure(
        self, conversation_id: ConversationId, exc: Exception
    ) -> AsyncIterator[TurnEvent]:
        detail = str(exc) or type(exc).__name__
        await self._repository.append_system(
            conversation_id, f"Ошибка подготовки контекста: {detail}"
        )
        yield TurnEvent(
            TurnState.FAILED,
            detail=detail,
            metadata={"error_type": "context_failed"},
        )

    async def _open_stream(
        self,
        command: TurnCommand,
        conversation: ResolvedConversation,
        prepared: PreparedContext,
    ) -> ModelStream:
        return await self._model.open_stream(
            ModelRequest(
                system=prepared.system,
                user=prepared.user,
                max_tokens=command.max_tokens,
                temperature=command.temperature,
                image_data_url=command.image_data_url,
                preferred_model=conversation.model,
                purpose=f"{command.surface.value}_conversation",
            )
        )

    async def _generate(
        self,
        command: TurnCommand,
        conversation: ResolvedConversation,
        user_message: ConversationMessage,
        stream: ModelStream,
    ) -> AsyncIterator[TurnEvent]:
        state = _GenerationState(
            started=self._clock.now(),
            last_flush=self._clock.now(),
        )
        yield TurnEvent(TurnState.GENERATING)
        try:
            async for event in self._consume(conversation.id, stream, state):
                yield event
        except asyncio.CancelledError:
            await self._persist_interrupted(conversation.id, state, stream.usage)
            raise
        except Exception as exc:
            await self._persist_interrupted(conversation.id, state, stream.usage)
            detail = str(exc) or type(exc).__name__
            await self._repository.append_system(
                conversation.id, f"Ошибка LLM: {detail}"
            )
            yield TurnEvent(TurnState.FAILED, detail=detail)
            return

        yield TurnEvent(TurnState.PERSISTING)
        result = await self._complete(
            command, conversation, user_message, stream.usage, state
        )
        yield TurnEvent(TurnState.COMPLETED, result=result)

    async def _consume(
        self,
        conversation_id: ConversationId,
        stream: ModelStream,
        state: _GenerationState,
    ) -> AsyncIterator[TurnEvent]:
        async for delta in stream.deltas():
            if not delta:
                continue
            state.chunks.append(delta)
            if state.assistant_id is None:
                state.assistant_id = await self._repository.begin_assistant(
                    conversation_id, provider=stream.usage.provider
                )
            now = self._clock.now()
            if now - state.last_flush >= self._partial_flush_seconds:
                await self._repository.update_assistant(state.assistant_id, state.text)
                state.last_flush = now
            yield TurnEvent(TurnState.GENERATING, text=delta)

    async def _complete(
        self,
        command: TurnCommand,
        conversation: ResolvedConversation,
        user_message: ConversationMessage,
        usage: ModelUsage,
        state: _GenerationState,
    ) -> TurnResult:
        answer = state.text.strip() or _EMPTY_ANSWER
        if state.assistant_id is None:
            state.assistant_id = await self._repository.begin_assistant(
                conversation.id, provider=usage.provider
            )
        elapsed_ms = self._elapsed_ms(state.started)
        await self._repository.finalize_assistant(
            state.assistant_id,
            answer,
            elapsed_ms=elapsed_ms,
            usage=usage,
        )
        result = TurnResult(
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            assistant_message_id=state.assistant_id,
            answer=answer,
            elapsed_ms=elapsed_ms,
            usage=usage,
        )
        await self._post_turn.dispatch(command, result)
        return result

    async def _persist_interrupted(
        self,
        conversation_id: ConversationId,
        state: _GenerationState,
        usage: ModelUsage,
    ) -> None:
        if state.assistant_id is None or not state.text:
            return
        await self._repository.finalize_assistant(
            state.assistant_id,
            state.text,
            elapsed_ms=self._elapsed_ms(state.started),
            usage=usage,
        )

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int((self._clock.now() - started) * 1000))


__all__ = ["ConversationService"]
