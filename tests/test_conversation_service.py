"""Conversation application tests with no database, web or Telegram runtime."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from app.application.chat import (
    ConversationMessage,
    ConversationService,
    ModelUsage,
    PreparedContext,
    ResolvedConversation,
    TurnCommand,
    TurnEvent,
    TurnResult,
)
from app.domains.chat import (
    ActorContext,
    ConversationAccessDenied,
    ConversationId,
    ConversationNotFound,
    ConversationSurface,
    ModelUnavailable,
    TenantId,
    TurnGenerationFailed,
    TurnState,
    UserId,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from app.application.chat.dto import ModelRequest


def _actor(user_id: int = 7) -> ActorContext:
    return ActorContext(
        tenant_id=TenantId(user_id),
        user_id=UserId(user_id),
        is_owner=True,
    )


def _command(
    *,
    surface: ConversationSurface = ConversationSurface.WEB,
    source_label: str | None = None,
) -> TurnCommand:
    return TurnCommand(
        actor=_actor(),
        surface=surface,
        conversation_id=ConversationId(11),
        text="remember this",
        source_label=source_label,
    )


@dataclass
class FakeRepository:
    conversation: ResolvedConversation | None = field(
        default_factory=lambda: ResolvedConversation(
            id=ConversationId(11),
            tenant_id=7,
            title="test",
            model="model-a",
        )
    )
    next_id: int = 100
    appended: list[tuple[str, str]] = field(default_factory=list)
    partials: list[tuple[int, str]] = field(default_factory=list)
    finalized: list[tuple[int, str, ModelUsage]] = field(default_factory=list)

    async def get(
        self, actor: ActorContext, conversation_id: ConversationId
    ) -> ResolvedConversation | None:
        if actor.tenant_id != TenantId(7) or conversation_id != ConversationId(11):
            return None
        return self.conversation

    async def append_user(
        self, conversation_id: ConversationId, content: str
    ) -> ConversationMessage:
        self.appended.append(("user", content))
        self.next_id += 1
        return ConversationMessage(self.next_id, "user", content)

    async def history(
        self,
        conversation_id: ConversationId,
        *,
        max_turns: int,
        exclude_message_id: int,
    ) -> tuple[ConversationMessage, ...]:
        return (ConversationMessage(1, "assistant", "previous"),)

    async def begin_assistant(
        self, conversation_id: ConversationId, *, provider: str | None
    ) -> int:
        self.next_id += 1
        return self.next_id

    async def update_assistant(self, message_id: int, content: str) -> None:
        self.partials.append((message_id, content))

    async def finalize_assistant(
        self,
        message_id: int,
        content: str,
        *,
        elapsed_ms: int,
        usage: ModelUsage,
    ) -> None:
        self.finalized.append((message_id, content, usage))

    async def append_system(
        self, conversation_id: ConversationId, content: str
    ) -> ConversationMessage:
        self.appended.append(("system", content))
        self.next_id += 1
        return ConversationMessage(self.next_id, "system", content)


@dataclass
class FakeContext:
    commands: list[TurnCommand] = field(default_factory=list)

    async def prepare(
        self,
        command: TurnCommand,
        conversation: ResolvedConversation,
        history: tuple[ConversationMessage, ...],
    ) -> PreparedContext:
        self.commands.append(command)
        return PreparedContext("same persona", command.model_text, history)


class FailingContext(FakeContext):
    async def prepare(
        self,
        command: TurnCommand,
        conversation: ResolvedConversation,
        history: tuple[ConversationMessage, ...],
    ) -> PreparedContext:
        raise RuntimeError("recall exploded")


class FakeStream:
    def __init__(
        self,
        chunks: tuple[str, ...] = ("hello", " world"),
        *,
        failure: Exception | None = None,
    ) -> None:
        self._chunks = chunks
        self._failure = failure
        self._usage = ModelUsage("fake", "model-a", 3, 2)

    @property
    def usage(self) -> ModelUsage:
        return self._usage

    async def _iterate(self) -> AsyncIterator[str]:
        for chunk in self._chunks:
            yield chunk
        if self._failure is not None:
            raise self._failure

    def deltas(self) -> AsyncIterator[str]:
        return self._iterate()


@dataclass
class FakeModel:
    stream: FakeStream = field(default_factory=FakeStream)
    requests: list[ModelRequest] = field(default_factory=list)

    async def open_stream(self, request: ModelRequest) -> FakeStream:
        self.requests.append(request)
        return self.stream


class UnavailableModel(FakeModel):
    async def open_stream(self, request: ModelRequest) -> FakeStream:
        raise ModelUnavailable("not configured")


@dataclass
class FakePostTurn:
    calls: list[tuple[TurnCommand, TurnResult]] = field(default_factory=list)

    async def dispatch(self, command: TurnCommand, result: TurnResult) -> None:
        self.calls.append((command, result))


def _service(
    repository: FakeRepository | None = None,
    context: FakeContext | None = None,
    model: FakeModel | None = None,
    post_turn: FakePostTurn | None = None,
) -> tuple[ConversationService, FakeRepository, FakeContext, FakeModel, FakePostTurn]:
    repo = repository or FakeRepository()
    context_port = context or FakeContext()
    model_port = model or FakeModel()
    post = post_turn or FakePostTurn()
    service = ConversationService(
        repo,
        context_port,
        model_port,
        post,
        partial_flush_seconds=0,
    )
    return service, repo, context_port, model_port, post


@pytest.mark.asyncio
async def test_web_and_telegram_share_one_turn_state_machine() -> None:
    service, repo, context, model, post = _service()

    web_events = [event async for event in service.stream_turn(_command())]
    telegram_events = [
        event
        async for event in service.stream_turn(
            _command(
                surface=ConversationSurface.TELEGRAM,
                source_label="Telegram · Owner",
            )
        )
    ]

    expected = [
        TurnState.ACCEPTED,
        TurnState.CONTEXT_READY,
        TurnState.GENERATING,
        TurnState.GENERATING,
        TurnState.GENERATING,
        TurnState.PERSISTING,
        TurnState.COMPLETED,
    ]
    assert [event.state for event in web_events] == expected
    assert [event.state for event in telegram_events] == expected
    assert context.commands[0].surface is ConversationSurface.WEB
    assert context.commands[1].surface is ConversationSurface.TELEGRAM
    assert repo.appended == [
        ("user", "remember this"),
        ("user", "[Telegram · Owner] remember this"),
    ]
    assert [request.system for request in model.requests] == [
        "same persona",
        "same persona",
    ]
    assert len(post.calls) == 2


@pytest.mark.asyncio
async def test_tenant_scope_is_checked_before_any_write_or_model_call() -> None:
    service, repo, _context, model, _post = _service()
    foreign = TurnCommand(
        actor=_actor(8),
        surface=ConversationSurface.WEB,
        conversation_id=ConversationId(11),
        text="secret",
    )

    with pytest.raises(ConversationNotFound):
        await service.handle_turn(foreign)

    assert repo.appended == []
    assert model.requests == []


@pytest.mark.asyncio
async def test_application_rejects_a_repository_tenant_scope_violation() -> None:
    repository = FakeRepository(
        conversation=ResolvedConversation(
            id=ConversationId(11),
            tenant_id=999,
            title="wrong tenant",
        )
    )
    service, repo, _context, model, _post = _service(repository=repository)

    with pytest.raises(ConversationAccessDenied, match="tenant mismatch"):
        await service.handle_turn(_command())

    assert repo.appended == []
    assert model.requests == []


@pytest.mark.asyncio
async def test_provider_failure_preserves_partial_and_records_terminal_error() -> None:
    model = FakeModel(FakeStream(("partial",), failure=RuntimeError("offline")))
    service, repo, _context, _model, post = _service(model=model)

    with pytest.raises(TurnGenerationFailed, match="offline"):
        await service.handle_turn(_command())

    assert repo.finalized[0][1] == "partial"
    assert repo.appended[-1] == ("system", "Ошибка LLM: offline")
    assert post.calls == []


@pytest.mark.asyncio
async def test_context_failure_is_terminal_and_never_calls_the_model() -> None:
    service, repo, _context, model, _post = _service(context=FailingContext())

    with pytest.raises(TurnGenerationFailed, match="recall exploded"):
        await service.handle_turn(_command())

    assert repo.appended[-1] == (
        "system",
        "Ошибка подготовки контекста: recall exploded",
    )
    assert model.requests == []


@pytest.mark.asyncio
async def test_unconfigured_model_keeps_its_typed_application_error() -> None:
    service, repo, _context, _model, _post = _service(model=UnavailableModel())

    with pytest.raises(ModelUnavailable, match="not configured"):
        await service.handle_turn(_command())

    assert repo.appended[-1] == ("system", "not configured")


class BlockingStream(FakeStream):
    def __init__(self, started: asyncio.Event) -> None:
        super().__init__(())
        self._started = started

    async def _iterate(self) -> AsyncIterator[str]:
        yield "durable partial"
        self._started.set()
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_cancellation_persists_already_generated_partial() -> None:
    started = asyncio.Event()
    service, repo, _context, _model, _post = _service(
        model=FakeModel(BlockingStream(started))
    )

    async def consume() -> None:
        async for _event in service.stream_turn(_command()):
            pass

    task = asyncio.create_task(consume())
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert repo.finalized[0][1] == "durable partial"


def test_domain_actor_rejects_cross_tenant_identity() -> None:
    with pytest.raises(ValueError, match="cross-tenant"):
        ActorContext(TenantId(1), UserId(2), is_owner=True)


def test_non_owner_cannot_request_private_context_or_tools() -> None:
    actor = ActorContext(TenantId(7), UserId(7), is_owner=False)
    with pytest.raises(ValueError, match="private context"):
        TurnCommand(
            actor=actor,
            surface=ConversationSurface.TELEGRAM,
            conversation_id=ConversationId(11),
            text="steal memory",
            include_private_context=True,
        )
    with pytest.raises(ValueError, match="tools"):
        TurnCommand(
            actor=actor,
            surface=ConversationSurface.TELEGRAM,
            conversation_id=ConversationId(11),
            text="run tool",
            include_private_context=False,
            allow_tools=True,
        )


@dataclass
class CapturingConversationService:
    commands: list[TurnCommand] = field(default_factory=list)

    async def handle_turn(self, command: TurnCommand) -> TurnResult:
        self.commands.append(command)
        return TurnResult(
            conversation_id=command.conversation_id,
            user_message_id=10,
            assistant_message_id=12,
            answer="shared answer",
            elapsed_ms=15,
            usage=ModelUsage(provider="fake", input_tokens=2, output_tokens=3),
        )


class CapturingStreamingService(CapturingConversationService):
    async def stream_turn(self, command: TurnCommand) -> AsyncIterator[TurnEvent]:
        result = await self.handle_turn(command)
        yield TurnEvent(TurnState.GENERATING, text="shared ")
        yield TurnEvent(TurnState.GENERATING, text="stream")
        yield TurnEvent(TurnState.COMPLETED, result=result)


@pytest.mark.asyncio
async def test_web_entrypoint_only_maps_http_to_shared_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.web.routes import chat_sessions  # noqa: PLC0415

    capture = CapturingConversationService()
    monkeypatch.setattr(chat_sessions, "_conversation_service", capture)

    async def owner(_user_id: int) -> bool:
        return True

    monkeypatch.setattr(chat_sessions, "is_owner", owner)
    response = await chat_sessions.api_send_message(
        session_id=11,
        session={"user_id": 7},  # type: ignore[typeddict-item]
        body={"question": "from web"},
    )

    assert json.loads(bytes(response.body))["assistant"]["content"] == "shared answer"
    assert len(capture.commands) == 1
    assert capture.commands[0].surface is ConversationSurface.WEB
    assert capture.commands[0].text == "from web"


@pytest.mark.asyncio
async def test_web_sse_presenter_serializes_shared_application_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.web.routes import chat_sessions  # noqa: PLC0415

    capture = CapturingStreamingService()
    monkeypatch.setattr(chat_sessions, "_conversation_service", capture)

    async def owner(_user_id: int) -> bool:
        return True

    monkeypatch.setattr(chat_sessions, "is_owner", owner)
    response = await chat_sessions._stream_via_conversation_service(
        session_id=11,
        user_id=7,
        question="stream from web",
        image_data_url=None,
    )
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else bytes(chunk).decode())
    payload = "".join(chunks)

    assert '"type": "delta", "text": "shared "' in payload
    assert '"type": "done"' in payload
    assert capture.commands[0].surface is ConversationSurface.WEB
    assert capture.commands[0].text == "stream from web"


@pytest.mark.asyncio
async def test_primary_web_sse_route_uses_service_in_simple_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.responses import StreamingResponse  # noqa: PLC0415

    from app.web.routes import chat_sessions  # noqa: PLC0415

    async def session(_user_id: int, _session_id: int) -> dict[str, Any]:
        return {"id": 11, "user_id": 7}

    async def flags() -> dict[str, bool]:
        return {"master": False}

    calls: list[dict[str, Any]] = []

    async def service_stream(**kwargs: Any) -> StreamingResponse:
        calls.append(kwargs)

        async def body() -> AsyncIterator[str]:
            yield 'data: {"type":"done"}\n\n'

        return StreamingResponse(body(), media_type="text/event-stream")

    monkeypatch.setattr(chat_sessions, "get_session", session)
    monkeypatch.setattr(chat_sessions, "get_advanced_flags", flags)
    monkeypatch.setattr(
        chat_sessions, "_stream_via_conversation_service", service_stream
    )
    response = await chat_sessions.api_send_stream(
        request=None,  # type: ignore[arg-type]
        session_id=11,
        session={"user_id": 7},  # type: ignore[typeddict-item]
        body={"question": "actual frontend path"},
    )

    assert isinstance(response, StreamingResponse)
    assert calls == [
        {
            "session_id": 11,
            "user_id": 7,
            "question": "actual frontend path",
            "image_data_url": None,
        }
    ]


class FakeTelegramMapping:
    async def session_id(self, chat_id: int) -> int | None:
        return 11

    async def clear_session_id(self, chat_id: int) -> None:
        return None


@pytest.mark.asyncio
async def test_telegram_entrypoint_maps_to_same_shared_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.integrations.telegram import service as telegram_service  # noqa: PLC0415

    capture = CapturingConversationService()

    async def session(_user_id: int, _session_id: int) -> dict[str, Any]:
        return {"id": 11}

    monkeypatch.setattr(telegram_service, "get_session", session)
    adapter = telegram_service.PersonaTelegramService(
        FakeTelegramMapping(),  # type: ignore[arg-type]
        conversation_service=capture,  # type: ignore[arg-type]
    )
    answer = await adapter.respond(
        persona_user_id=7,
        telegram_chat_id=99,
        question="from Telegram",
        chat_title="DM",
        sender_label="Owner",
        include_private_context=True,
    )

    assert answer == "shared answer"
    assert len(capture.commands) == 1
    command = capture.commands[0]
    assert command.surface is ConversationSurface.TELEGRAM
    assert command.source_label == "Telegram · Owner"
    assert command.include_private_context is True
    assert command.allow_tools is False
