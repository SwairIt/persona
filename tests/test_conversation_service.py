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
    ToolCall,
    ToolExecution,
    ToolTurnPolicy,
    TurnCommand,
    TurnEvent,
    TurnResult,
    is_valid_tool_wire_name,
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


class CapturingToolStreamingService(CapturingConversationService):
    async def stream_turn(self, command: TurnCommand) -> AsyncIterator[TurnEvent]:
        self.commands.append(command)
        yield TurnEvent(
            TurnState.TOOL_RUNNING,
            metadata={"name": "read_one", "round": 1, "call": 1},
        )
        yield TurnEvent(
            TurnState.TOOL_COMPLETED,
            text="must not leak",
            metadata={
                "name": "read_one",
                "status": "done",
                "round": 1,
                "call": 1,
                "truncated": False,
                "elapsed_ms": 25,
            },
        )
        yield TurnEvent(TurnState.GENERATING, text="safe answer")
        yield TurnEvent(
            TurnState.COMPLETED,
            result=TurnResult(
                conversation_id=command.conversation_id,
                user_message_id=10,
                assistant_message_id=12,
                answer="safe answer",
                elapsed_ms=30,
                usage=ModelUsage(provider="fake"),
            ),
        )


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
async def test_web_sse_presenter_preserves_safe_tool_frame_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.web.routes import chat_sessions  # noqa: PLC0415

    capture = CapturingToolStreamingService()
    monkeypatch.setattr(chat_sessions, "_conversation_service", capture)

    async def owner(_user_id: int) -> bool:
        return True

    monkeypatch.setattr(chat_sessions, "is_owner", owner)
    response = await chat_sessions._stream_via_conversation_service(
        session_id=11,
        user_id=7,
        question="tool from web",
        image_data_url=None,
        allow_tools=True,
        tool_policy=ToolTurnPolicy(max_rounds=2, max_calls=2),
    )
    chunks: list[str] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, str) else bytes(chunk).decode())
    payload = "".join(chunks)

    assert '"type": "tool_call"' in payload
    assert '"type": "tool_result"' in payload
    assert '"name": "read_one"' in payload
    assert '"status": "done"' in payload
    assert '"text": "safe answer"' in payload
    assert "must not leak" not in payload
    assert capture.commands[0].allow_tools is True
    assert capture.commands[0].tool_policy == ToolTurnPolicy(
        max_rounds=2,
        max_calls=2,
    )


@pytest.mark.asyncio
async def test_web_sse_tool_request_is_downgraded_for_non_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.web.routes import chat_sessions  # noqa: PLC0415

    capture = CapturingStreamingService()
    monkeypatch.setattr(chat_sessions, "_conversation_service", capture)

    async def non_owner(_user_id: int) -> bool:
        return False

    monkeypatch.setattr(chat_sessions, "is_owner", non_owner)
    response = await chat_sessions._stream_via_conversation_service(
        session_id=11,
        user_id=7,
        question="do not execute",
        image_data_url=None,
        allow_tools=True,
        tool_policy=ToolTurnPolicy(max_rounds=2, max_calls=2),
    )
    async for _chunk in response.body_iterator:
        pass

    assert capture.commands[0].actor.is_owner is False
    assert capture.commands[0].include_private_context is False
    assert capture.commands[0].allow_tools is False
    assert capture.commands[0].tool_policy is None


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

    async def stop(_session_id: int, _on: bool) -> None:
        return None

    calls: list[dict[str, Any]] = []

    async def service_stream(**kwargs: Any) -> StreamingResponse:
        calls.append(kwargs)

        async def body() -> AsyncIterator[str]:
            yield 'data: {"type":"done"}\n\n'

        return StreamingResponse(body(), media_type="text/event-stream")

    monkeypatch.setattr(chat_sessions, "get_session", session)
    monkeypatch.setattr(chat_sessions, "get_advanced_flags", flags)
    monkeypatch.setattr(chat_sessions, "_set_stop", stop)
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
    assert len(calls) == 1
    live_gen = calls[0].pop("live_gen")
    assert isinstance(live_gen, chat_sessions._LiveGen)
    assert calls[0] == {
        "session_id": 11,
        "user_id": 7,
        "question": "actual frontend path",
        "image_data_url": None,
    }
    chat_sessions._LIVE_GENS.pop(11, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode_name", ["auto", "bypass"])
async def test_advanced_auto_and_bypass_keep_full_legacy_context_parity(
    monkeypatch: pytest.MonkeyPatch,
    mode_name: str,
) -> None:
    from fastapi.responses import StreamingResponse  # noqa: PLC0415

    from app import chat, mcp  # noqa: PLC0415
    from app.chat import auto_prompts, commands, user_memory  # noqa: PLC0415
    from app.skills import store as skill_store  # noqa: PLC0415
    from app.web.routes import chat_sessions  # noqa: PLC0415

    async def session(_user_id: int, _session_id: int) -> dict[str, Any]:
        return {
            "id": 11,
            "user_id": 7,
            "provider": "ollama",
            "model": "text-model",
            "summary": "SUMMARY_MARKER",
            "custom_system_prompt": "CUSTOM_PERSONA_MARKER",
        }

    async def flags() -> dict[str, bool]:
        return {
            "master": True,
            "effort": True,
            "modes": True,
            "tools": True,
            "auto_prompt": True,
            "choices": True,
        }

    async def mode(_session_id: int) -> str:
        return mode_name

    async def effort(_session_id: int) -> str:
        return "deep"

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    history = [
        {
            "role": "user" if index % 2 == 0 else "assistant",
            "content": f"HISTORY_{index}",
        }
        for index in range(16)
    ]

    async def get_history(_session_id: int, *, max_turns: int) -> list[dict[str, str]]:
        assert max_turns == 20
        return history

    async def auto_prompt_on() -> bool:
        return True

    async def recall_mode() -> str:
        return "generative"

    recall_calls: list[tuple[int, str, int | None, bool]] = []

    async def hybrid(
        user_id: int,
        question: str,
        exclude_session_id: int | None = None,
        limit: int = 6,
        *,
        salience: bool = False,
    ) -> str:
        del limit
        recall_calls.append((user_id, question, exclude_session_id, salience))
        return "RECALL_MARKER"

    advertised: list[str] = []

    async def enabled_names() -> list[str]:
        return ["read_file", "write_file"]

    def tools_prompt(names: list[str]) -> str:
        advertised.extend(names)
        return "\nTOOLS_MARKER"

    async def memory(_user_id: int) -> str:
        return "USER_MEMORY_MARKER"

    async def skills(_user_id: int) -> str:
        return "\nSKILLS_MARKER"

    async def activity(_question: str, budget_chars: int = 0) -> str:
        del budget_chars
        return "ACTIVITY_MARKER"

    async def pins(_session_id: int) -> list[dict[str, str]]:
        return [{"role": "user", "content": "PIN_MARKER"}]

    async def reaction(_session_id: int) -> str:
        return "error"

    async def vision(_provider: str | None) -> str:
        return "vision-model"

    async def owner(_user_id: int) -> bool:
        return True

    requests: list[Any] = []

    class FakeLegacyClient:
        provider = "fake"

        def __init__(self) -> None:
            self._inner = self
            self._model = "initial"
            self.last_input_tokens = 10
            self.last_output_tokens = 2

        async def stream(self, request: Any) -> AsyncIterator[str]:
            requests.append(request)
            yield "ready"

    fake_client = FakeLegacyClient()

    async def begin(
        _session_id: int,
        _role: str,
        *,
        model_used: str | None,
    ) -> int:
        del model_used
        return 91

    async def append(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"id": 90}

    async def forbidden_service(**_kwargs: Any) -> StreamingResponse:
        raise AssertionError("advanced context must not use the reduced presenter")

    monkeypatch.setattr(auto_prompts, "detect_overlay", lambda _q: "\nAUTO_MARKER")
    monkeypatch.setattr(commands, "command_overlay", lambda _cmd: "COMMAND_MARKER")
    monkeypatch.setattr(user_memory, "build_memory_block", memory)
    monkeypatch.setattr(chat, "hybrid_recall", hybrid)
    monkeypatch.setattr(mcp, "all_enabled_tool_names", enabled_names)
    monkeypatch.setattr(mcp, "build_tools_prompt", tools_prompt)
    monkeypatch.setattr(skill_store, "enabled_skills_prompt", skills)
    monkeypatch.setattr("app.memory_context.build_memory_context", activity)
    monkeypatch.setattr(chat_sessions, "get_session", session)
    monkeypatch.setattr(chat_sessions, "get_advanced_flags", flags)
    monkeypatch.setattr(chat_sessions, "_get_mode", mode)
    monkeypatch.setattr(chat_sessions, "_get_effort", effort)
    monkeypatch.setattr(chat_sessions, "_get_auto_prompt", auto_prompt_on)
    monkeypatch.setattr(chat_sessions, "_get_recall_mode", recall_mode)
    monkeypatch.setattr(chat_sessions, "_find_vision_model_for_provider", vision)
    monkeypatch.setattr(chat_sessions, "is_owner", owner)
    monkeypatch.setattr(chat_sessions, "_set_stop", noop)
    monkeypatch.setattr(chat_sessions, "append_message", append)
    monkeypatch.setattr(chat_sessions, "touch_session", noop)
    monkeypatch.setattr(chat_sessions, "build_history_for_llm", get_history)
    monkeypatch.setattr(chat_sessions, "get_pinned_messages", pins)
    monkeypatch.setattr(chat_sessions, "latest_reaction", reaction)
    monkeypatch.setattr(chat_sessions, "start_streaming_message", begin)
    monkeypatch.setattr(chat_sessions, "update_streaming_message", noop)
    monkeypatch.setattr(chat_sessions, "finalize_streaming_message", noop)
    monkeypatch.setattr(chat_sessions, "maybe_summarise", noop)
    monkeypatch.setattr(chat_sessions, "make_client", lambda **_kwargs: fake_client)
    monkeypatch.setattr(
        chat_sessions,
        "_stream_via_conversation_service",
        forbidden_service,
    )
    response = await chat_sessions.api_send_stream(
        request=None,  # type: ignore[arg-type]
        session_id=11,
        session={"user_id": 7},  # type: ignore[typeddict-item]
        body={
            "question": "advanced context question",
            "cmd": "review",
            "image_data_url": "data:image/png;base64,AA==",
        },
    )

    assert isinstance(response, StreamingResponse)
    async for _chunk in response.body_iterator:
        pass

    assert len(requests) == 1
    model_request = requests[0]
    assert model_request.user == "advanced context question"
    assert model_request.max_tokens == chat_sessions._EFFORT_TOKENS["deep"]
    assert model_request.temperature == chat_sessions._EFFORT_TEMP["deep"]
    assert fake_client._model == "vision-model"
    assert recall_calls == [
        (7, "advanced context question", 11, True),
    ]
    assert advertised == ["read_file"]
    for marker in (
        "CUSTOM_PERSONA_MARKER",
        "прикреплено изображение",
        "AUTO_MARKER",
        "COMMAND_MARKER",
        "USER_MEMORY_MARKER",
        "RECALL_MARKER",
        "TOOLS_MARKER",
        "SKILLS_MARKER",
        "SUMMARY_MARKER",
        "PIN_MARKER",
        "перепроверь факты",
        "ACTIVITY_MARKER",
        "Напоминание: держи свою роль",
        "HISTORY_0",
    ):
        assert marker in model_request.system
    assert (
        "РЕЖИМ: АВТО" if mode_name == "auto" else "РЕЖИМ: БЕЗ СПРОСА"  # noqa: RUF001
    ) in model_request.system


def test_advanced_legacy_tool_allowlist_is_read_only_and_wire_safe() -> None:
    from app.web.routes.chat_sessions import (  # noqa: PLC0415
        _safe_autonomous_tool_names,
    )

    assert _safe_autonomous_tool_names(
        [
            "read_file",
            "write_file",
            "run_shell",
            "delete_path",
            "mcp__server__external",
            "read-file",
        ]
    ) == frozenset({"read_file"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("initial_output", "expected_answer", "expected_calls", "owner_allowed"),
    [
        (
            '<tool>read_file({"path":"SECRET_ARG"})</tool>',
            "final safe answer",
            1,
            True,
        ),
        (
            '<tool>write_file({"content":"SECRET_ARG"})</tool>',
            "Tool operation could not be completed safely.",
            0,
            True,
        ),
        (
            '<tool>bad-name({"secret":"SECRET_ARG"})</tool>',
            "Tool operation could not be completed safely.",
            0,
            True,
        ),
        (
            '<tool>read_file({"path":"SECRET_ARG"})</tool>',
            "Tool operation could not be completed safely.",
            0,
            False,
        ),
        (
            "ordinary non-owner answer",
            "ordinary non-owner answer",
            0,
            False,
        ),
    ],
)
async def test_advanced_legacy_tools_never_publish_or_persist_raw_intent(
    monkeypatch: pytest.MonkeyPatch,
    initial_output: str,
    expected_answer: str,
    expected_calls: int,
    owner_allowed: bool,
) -> None:
    from fastapi.responses import StreamingResponse  # noqa: PLC0415

    from app import activity as activity_module  # noqa: PLC0415
    from app import mcp  # noqa: PLC0415
    from app.chat import user_memory  # noqa: PLC0415
    from app.skills import store as skill_store  # noqa: PLC0415
    from app.web.routes import chat_sessions, live_sse  # noqa: PLC0415

    async def session(_user_id: int, _session_id: int) -> dict[str, Any]:
        return {
            "id": 11,
            "user_id": 7,
            "provider": "fake",
            "model": "fake-model",
            "summary": None,
            "custom_system_prompt": "private tool persona",
        }

    async def flags() -> dict[str, bool]:
        return {
            "master": True,
            "effort": True,
            "modes": True,
            "tools": True,
            "auto_prompt": False,
            "choices": False,
        }

    async def noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def empty(*_args: Any, **_kwargs: Any) -> str:
        return ""

    registry_reads = 0

    async def enabled_names() -> list[str]:
        nonlocal registry_reads
        registry_reads += 1
        return ["read_file", "write_file"]

    calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(
        name: str,
        arguments: dict[str, Any],
        *,
        user_id: int,
        session_id: int,
    ) -> str:
        assert (user_id, session_id) == (7, 11)
        calls.append((name, arguments))
        return "SECRET_RESULT"

    requests: list[Any] = []

    class FakeClient:
        provider = "fake"
        last_input_tokens = 1
        last_output_tokens = 1

        def __init__(self) -> None:
            self._inner = self
            self._model = "fake-model"

        async def stream(self, request: Any) -> AsyncIterator[str]:
            requests.append(request)
            yield initial_output if len(requests) == 1 else "final safe answer"

    finalized: list[str] = []
    incremental_updates: list[str] = []

    async def finalize(
        _message_id: int,
        content: str,
        **_kwargs: Any,
    ) -> None:
        finalized.append(content)

    async def begin(*_args: Any, **_kwargs: Any) -> int:
        return 91

    async def update(_message_id: int, content: str) -> None:
        incremental_updates.append(content)

    async def append(*_args: Any, **_kwargs: Any) -> dict[str, int]:
        return {"id": 90}

    async def no_pins(_session_id: int) -> list[dict[str, str]]:
        return []

    async def no_reaction(_session_id: int) -> str:
        return ""

    async def mode(_session_id: int) -> str:
        return "auto"

    async def effort(_session_id: int) -> str:
        return "fast"

    async def not_stopped(_session_id: int) -> bool:
        return False

    async def owner(_user_id: int) -> bool:
        return owner_allowed

    async def begin_execution(*_args: Any, **_kwargs: Any) -> int:
        assert _args[2] == {}
        return 501

    monkeypatch.setattr(mcp, "all_enabled_tool_names", enabled_names)
    monkeypatch.setattr(mcp, "build_tools_prompt", lambda _names: "\nTOOLS")
    monkeypatch.setattr(mcp, "call_tool", call_tool)
    monkeypatch.setattr(user_memory, "build_memory_block", empty)
    monkeypatch.setattr(skill_store, "enabled_skills_prompt", empty)
    monkeypatch.setattr("app.memory_context.build_memory_context", empty)
    monkeypatch.setattr(activity_module, "start_execution", begin_execution)
    monkeypatch.setattr(activity_module, "finish_execution", noop)
    monkeypatch.setattr(live_sse, "publish_activity", noop)
    monkeypatch.setattr(chat_sessions, "get_session", session)
    monkeypatch.setattr(chat_sessions, "get_advanced_flags", flags)
    monkeypatch.setattr(chat_sessions, "_get_mode", mode)
    monkeypatch.setattr(chat_sessions, "_get_effort", effort)
    monkeypatch.setattr(chat_sessions, "_get_recall_mode", empty)
    monkeypatch.setattr(chat_sessions, "_set_stop", noop)
    monkeypatch.setattr(chat_sessions, "_is_stopped", not_stopped)
    monkeypatch.setattr(chat_sessions, "is_owner", owner)
    monkeypatch.setattr(chat_sessions, "append_message", append)
    monkeypatch.setattr(chat_sessions, "touch_session", noop)
    monkeypatch.setattr(chat_sessions, "build_history_for_llm", lambda *_a, **_k: empty())
    monkeypatch.setattr(chat_sessions, "get_pinned_messages", no_pins)
    monkeypatch.setattr(chat_sessions, "latest_reaction", no_reaction)
    monkeypatch.setattr(chat_sessions, "start_streaming_message", begin)
    monkeypatch.setattr(chat_sessions, "update_streaming_message", update)
    monkeypatch.setattr(chat_sessions, "finalize_streaming_message", finalize)
    monkeypatch.setattr(chat_sessions, "maybe_summarise", noop)
    monkeypatch.setattr(chat_sessions, "make_client", lambda **_kwargs: FakeClient())

    response = await chat_sessions.api_send_stream(
        request=None,  # type: ignore[arg-type]
        session_id=11,
        session={"user_id": 7},  # type: ignore[typeddict-item]
        body={"question": "use a private tool"},
    )
    assert isinstance(response, StreamingResponse)
    public = ""
    async for chunk in response.body_iterator:
        public += chunk if isinstance(chunk, str) else bytes(chunk).decode()
    # Let the route's best-effort summary/memory/index tasks close their
    # SQLite connections before pytest tears down this parametrized loop.
    await asyncio.sleep(0.05)

    assert len(calls) == expected_calls
    assert registry_reads == (2 if expected_calls else int(owner_allowed))
    assert expected_answer in public
    assert finalized == [expected_answer]
    assert incremental_updates == []
    for secret in ("<tool>", "</tool>", "SECRET_ARG", "SECRET_RESULT"):
        assert secret not in public
        assert secret not in finalized[0]
    if expected_calls:
        assert '"type": "tool_call"' in public
        assert '"type": "tool_result"' in public
        assert '"args": {}' in public
        assert '"result": ""' in public


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
    assert command.source_label == "Telegram owner · Owner"
    assert command.include_private_context is True
    assert command.allow_tools is False
    # Никакого «краткого» потолка: он рубил ответ на полуслове. Краткость —
    # дело системного промпта, не счётчика токенов.
    assert command.max_tokens == 2048


@pytest.mark.asyncio
async def test_telegram_identity_context_survives_forty_people(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Справочник участников не должен обрываться на середине JSON."""
    from app.integrations.telegram import service as telegram_service  # noqa: PLC0415

    identity = "AUTHORITATIVE CURRENT TELEGRAM TURN:\n" + "\n".join(
        f'{{"telegram_user_id":{1000 + i},"display_name":"Участник {i}"}}'
        for i in range(40)
    )
    assert len(identity) > 2_000

    capture = CapturingConversationService()

    async def session(_user_id: int, _session_id: int) -> dict[str, Any]:
        return {"id": 11}

    monkeypatch.setattr(telegram_service, "get_session", session)
    adapter = telegram_service.PersonaTelegramService(
        FakeTelegramMapping(),  # type: ignore[arg-type]
        conversation_service=capture,  # type: ignore[arg-type]
    )
    await adapter.respond(
        persona_user_id=7,
        telegram_chat_id=42,
        question="кто здесь есть?",
        chat_title="Личка",
        sender_label="Owner",
        is_owner=True,
        trusted_identity_context=identity,
    )
    passed = capture.commands[0].metadata["telegram_identity_context"]
    assert passed == identity, "identity context не должен обрезаться в адаптере"


@pytest.mark.asyncio
async def test_telegram_prepare_keeps_full_identity_and_wider_transcript(db) -> None:
    """Exercise the real prompt assembly for a Telegram TurnCommand.

    Guards the two call sites that only the metadata-slice test above does not
    cover: `PersonaContextAdapter.prepare` (legacy.py:229, the
    `<TRUSTED_TELEGRAM_IDENTITY>` interpolation) and `_bounded_transcript`
    (legacy.py:240, the Telegram transcript bound). A typo in either limit
    would pass every other test silently.
    """
    from app.adapters.conversation.legacy import PersonaContextAdapter  # noqa: PLC0415

    await db.execute(
        "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(?,?,?)",
        (7, "7@example.test", "x"),
    )
    await db.commit()

    identity = "AUTHORITATIVE CURRENT TELEGRAM TURN:\n" + "\n".join(
        f'{{"telegram_user_id":{1000 + i},"display_name":"Участник {i}"}}'
        for i in range(40)
    )
    assert len(identity) > 2_000

    command = TurnCommand(
        actor=_actor(),
        surface=ConversationSurface.TELEGRAM,
        conversation_id=ConversationId(11),
        text="кто здесь есть?",
        include_private_context=False,
        allow_tools=False,
        metadata={"telegram_identity_context": identity},
    )
    conversation = ResolvedConversation(id=ConversationId(11), tenant_id=7, title="chat")
    # Three messages whose combined transcript is over the old 800-char bound
    # but under the new 6_000-char one. Under the old bound only the two most
    # recent would survive (reversed-history packing stops once the running
    # total exceeds max_chars); MARKER_OLDEST would be the one dropped.
    history = tuple(
        ConversationMessage(id=i, role="user", content=f"MARKER_{i} " + "y" * 340)
        for i in range(3)
    )

    prepared = await PersonaContextAdapter().prepare(command, conversation, history)

    assert (
        f"<TRUSTED_TELEGRAM_IDENTITY>\n{identity}\n</TRUSTED_TELEGRAM_IDENTITY>"
        in prepared.system
    ), "identity context must reach the system prompt intact"
    assert "MARKER_0" in prepared.system, (
        "oldest message was dropped -- the Telegram transcript bound "
        "regressed to (or below) the old 800-char cap"
    )
    assert "MARKER_1" in prepared.system
    assert "MARKER_2" in prepared.system


@pytest.mark.asyncio
async def test_telegram_group_wraps_transcript_as_untrusted(db) -> None:
    """A Telegram GROUP turn wraps history and carries the ignore-as-command rule.

    Regression guard for the surface-vs-group-ness confusion fixed after
    commit 2bbfede: the wrapping and the accompanying rules sentence must key
    off `include_private_context`, not merely off the Telegram surface.
    """
    from app.adapters.conversation.legacy import PersonaContextAdapter  # noqa: PLC0415

    await db.execute(
        "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(?,?,?)",
        (7, "7@example.test", "x"),
    )
    await db.commit()

    command = TurnCommand(
        actor=_actor(),
        surface=ConversationSurface.TELEGRAM,
        conversation_id=ConversationId(11),
        text="кто здесь есть?",
        include_private_context=False,
        allow_tools=False,
    )
    conversation = ResolvedConversation(id=ConversationId(11), tenant_id=7, title="chat")
    history = (ConversationMessage(id=1, role="user", content="GROUP_MARKER hi"),)

    prepared = await PersonaContextAdapter().prepare(command, conversation, history)

    assert "<UNTRUSTED_GROUP_TRANSCRIPT>" in prepared.system
    assert "</UNTRUSTED_GROUP_TRANSCRIPT>" in prepared.system
    assert "GROUP_MARKER hi" in prepared.system
    assert prepared.system.index(
        "<UNTRUSTED_GROUP_TRANSCRIPT>"
    ) < prepared.system.index("GROUP_MARKER hi") < prepared.system.index(
        "</UNTRUSTED_GROUP_TRANSCRIPT>"
    ), "group transcript content must be inside the untrusted-transcript wrapper"
    assert "UNTRUSTED_GROUP_TRANSCRIPT> — это чужие" in prepared.system, (
        "group turn must still carry the ignore-transcript-as-command rule"
    )


@pytest.mark.asyncio
async def test_telegram_private_owner_transcript_not_wrapped_as_untrusted(db) -> None:
    """The owner's private Telegram DM must not be wrapped as a group transcript.

    Regression test: `include_private_context=True` (owner 1:1 DM) previously
    hit the same branch as group turns, wrapping the owner's own history in
    `<UNTRUSTED_GROUP_TRANSCRIPT>` and telling the model to ignore it as a
    command -- which broke multi-turn owner instructions like "answer shorter"
    on the very next turn.
    """
    from app.adapters.conversation.legacy import PersonaContextAdapter  # noqa: PLC0415

    await db.execute(
        "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(?,?,?)",
        (7, "7@example.test", "x"),
    )
    await db.commit()

    command = TurnCommand(
        actor=_actor(),
        surface=ConversationSurface.TELEGRAM,
        conversation_id=ConversationId(11),
        text="отвечай короче",
        include_private_context=True,
        allow_tools=False,
    )
    conversation = ResolvedConversation(id=ConversationId(11), tenant_id=7, title="chat")
    history = (ConversationMessage(id=1, role="user", content="PRIVATE_MARKER hi"),)

    prepared = await PersonaContextAdapter().prepare(command, conversation, history)

    assert "PRIVATE_MARKER hi" in prepared.system, (
        "owner DM transcript content must still reach the prompt"
    )
    assert "<UNTRUSTED_GROUP_TRANSCRIPT>" not in prepared.system, (
        "owner's own DM history must not be wrapped as an untrusted group transcript"
    )
    assert "UNTRUSTED_GROUP_TRANSCRIPT> — это чужие" not in prepared.system, (
        "private owner turn must not reference a tag that isn't in the prompt"
    )


@pytest.mark.asyncio
async def test_web_surface_transcript_unaffected_by_telegram_gating(db) -> None:
    """Non-Telegram (web) surfaces are unaffected by the group-vs-DM gating."""
    from app.adapters.conversation.legacy import PersonaContextAdapter  # noqa: PLC0415

    await db.execute(
        "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(?,?,?)",
        (7, "7@example.test", "x"),
    )
    await db.commit()

    command = TurnCommand(
        actor=_actor(),
        surface=ConversationSurface.WEB,
        conversation_id=ConversationId(11),
        text="hello",
        include_private_context=True,
        allow_tools=False,
    )
    conversation = ResolvedConversation(id=ConversationId(11), tenant_id=7, title="chat")
    history = (ConversationMessage(id=1, role="user", content="WEB_MARKER hi"),)

    prepared = await PersonaContextAdapter().prepare(command, conversation, history)

    assert "WEB_MARKER hi" in prepared.system
    assert "<UNTRUSTED_GROUP_TRANSCRIPT>" not in prepared.system
    assert "UNTRUSTED_GROUP_TRANSCRIPT> — это чужие" not in prepared.system


@dataclass
class SequencedModel:
    streams: list[FakeStream]
    requests: list[ModelRequest] = field(default_factory=list)

    async def open_stream(self, request: ModelRequest) -> FakeStream:
        self.requests.append(request)
        if not self.streams:
            raise AssertionError("unexpected extra model turn")
        return self.streams.pop(0)


@dataclass
class RepeatingRepository(FakeRepository):
    phrase: str = "Клод, сам. На твоем счету теперь еще одна победа."

    async def history(
        self,
        conversation_id: ConversationId,
        *,
        max_turns: int,
        exclude_message_id: int,
    ) -> tuple[ConversationMessage, ...]:
        return (ConversationMessage(1, "assistant", self.phrase),)


@pytest.mark.asyncio
async def test_telegram_retries_repeated_answer_before_persisting() -> None:
    repo = RepeatingRepository()
    model = SequencedModel(
        [
            FakeStream((repo.phrase,)),
            FakeStream(("Всё, молчу.",)),
        ]
    )
    service = ConversationService(
        repo,
        FakeContext(),
        model,
        FakePostTurn(),
        partial_flush_seconds=0,
    )

    result = await service.handle_turn(
        _command(surface=ConversationSurface.TELEGRAM)
    )

    assert result.answer == "Всё, молчу."
    assert len(model.requests) == 2
    assert model.requests[1].purpose == "telegram_anti_repeat_conversation"
    assert "ANTI_REPEAT_RETRY" in model.requests[1].system
    assert [entry[1] for entry in repo.finalized] == ["Всё, молчу."]


@dataclass
class FakeTools:
    approved: frozenset[str] = frozenset(
        {"read_one", "read_two", "read_three"}
    )
    calls: list[ToolCall] = field(default_factory=list)
    output: str = "raw secret tool output"

    async def approved_tool_names(
        self,
        command: TurnCommand,
    ) -> frozenset[str]:
        assert command.actor.is_owner
        return self.approved

    def parse_calls(self, text: str) -> tuple[ToolCall, ...]:
        calls: list[ToolCall] = []
        for name in ("read_one", "read_two", "read_three", "not_approved"):
            marker = f"<tool>{name}</tool>"
            if marker in text:
                calls.append(ToolCall(name, {}, marker))
        return tuple(calls)

    async def execute(
        self,
        command: TurnCommand,
        call: ToolCall,
    ) -> ToolExecution:
        assert command.allow_tools
        self.calls.append(call)
        return ToolExecution(call, self.output)


def _tool_command(surface: ConversationSurface) -> TurnCommand:
    return TurnCommand(
        actor=_actor(),
        surface=surface,
        conversation_id=ConversationId(11),
        text="use the shared capability",
        allow_tools=True,
        tool_policy=ToolTurnPolicy(max_rounds=3, max_calls=3),
    )


@pytest.mark.parametrize(
    "surface",
    [ConversationSurface.WEB, ConversationSurface.TELEGRAM],
)
@pytest.mark.asyncio
async def test_tool_turn_has_web_telegram_parity_without_leaking_internals(
    surface: ConversationSurface,
) -> None:
    repo = FakeRepository()
    context = FakeContext()
    model = SequencedModel(
        [
            FakeStream(("<tool>read_one</tool>",)),
            FakeStream(("final ", "answer")),
        ]
    )
    post = FakePostTurn()
    tools = FakeTools()
    service = ConversationService(
        repo,
        context,
        model,
        post,
        tools=tools,
        partial_flush_seconds=0,
    )

    events = [event async for event in service.stream_turn(_tool_command(surface))]
    result = events[-1].result

    assert result is not None
    assert result.answer == "final answer"
    assert result.usage.input_tokens == 6
    assert result.usage.output_tokens == 4
    assert repo.appended == [("user", "use the shared capability")]
    assert [entry[1] for entry in repo.finalized] == ["final answer"]
    assert len(post.calls) == 1
    assert [call.name for call in tools.calls] == ["read_one"]
    assert len(model.requests) == 2
    assert model.requests[0].system == model.requests[1].system == "same persona"
    assert "UNTRUSTED DATA" in model.requests[1].user
    assert tools.output in model.requests[1].user
    encoded_context = model.requests[1].user.split(
        "<UNTRUSTED_TOOL_CONTEXT_JSON>\n",
        1,
    )[1].split("\n</UNTRUSTED_TOOL_CONTEXT_JSON>", 1)[0]
    followup_context = json.loads(encoded_context)
    assert followup_context["original_user_request"] == (
        "use the shared capability"
    )
    assert followup_context["prior_assistant_tool_intent"] == (
        "<tool>read_one</tool>"
    )
    public_text = "".join(event.text for event in events)
    assert public_text == "final answer"
    assert "<tool>" not in public_text
    assert tools.output not in public_text
    tool_events = [
        event
        for event in events
        if event.state in {TurnState.TOOL_RUNNING, TurnState.TOOL_COMPLETED}
    ]
    assert tool_events
    assert all(event.text == "" for event in tool_events)
    assert all("arguments" not in event.metadata for event in tool_events)


@dataclass
class TaskAwareModel:
    requests: list[ModelRequest] = field(default_factory=list)

    async def open_stream(self, request: ModelRequest) -> FakeStream:
        self.requests.append(request)
        if len(self.requests) == 1:
            return FakeStream(("<tool>read_one</tool>",))
        encoded = request.user.split(
            "<UNTRUSTED_TOOL_CONTEXT_JSON>\n",
            1,
        )[1].split("\n</UNTRUSTED_TOOL_CONTEXT_JSON>", 1)[0]
        context = json.loads(encoded)
        task = str(context["original_user_request"])
        intent = str(context["prior_assistant_tool_intent"])
        if task == "answer the original weather task" and "read_one" in intent:
            return FakeStream(("weather task understood",))
        return FakeStream(("task context missing",))


@pytest.mark.asyncio
async def test_followup_model_receives_original_task_and_prior_tool_intent() -> None:
    model = TaskAwareModel()
    service = ConversationService(
        FakeRepository(),
        FakeContext(),
        model,
        FakePostTurn(),
        tools=FakeTools(),
    )
    command = TurnCommand(
        actor=_actor(),
        surface=ConversationSurface.TELEGRAM,
        conversation_id=ConversationId(11),
        text="answer the original weather task",
        allow_tools=True,
    )

    result = await service.handle_turn(command)

    assert result.answer == "weather task understood"
    assert len(model.requests) == 2


@pytest.mark.asyncio
async def test_tool_loop_deduplicates_repeated_calls_and_fails_closed() -> None:
    repo = FakeRepository()
    model = SequencedModel(
        [
            FakeStream(("<tool>read_one</tool>",)),
            FakeStream(("<tool>read_one</tool>",)),
        ]
    )
    tools = FakeTools()
    service = ConversationService(
        repo,
        FakeContext(),
        model,
        FakePostTurn(),
        tools=tools,
    )

    result = await service.handle_turn(_tool_command(ConversationSurface.TELEGRAM))

    assert [call.name for call in tools.calls] == ["read_one"]
    assert len(model.requests) == 2
    assert result.answer.startswith("Не удалось безопасно")  # noqa: RUF001
    assert "<tool>" not in result.answer
    assert [entry[1] for entry in repo.finalized] == [result.answer]


@pytest.mark.asyncio
async def test_tool_loop_obeys_round_and_call_bounds() -> None:
    repo = FakeRepository()
    model = SequencedModel(
        [
            FakeStream(("<tool>read_one</tool>",)),
            FakeStream(("<tool>read_two</tool>",)),
            FakeStream(("<tool>read_three</tool>",)),
        ]
    )
    tools = FakeTools()
    service = ConversationService(
        repo,
        FakeContext(),
        model,
        FakePostTurn(),
        tools=tools,
    )
    command = TurnCommand(
        actor=_actor(),
        surface=ConversationSurface.WEB,
        conversation_id=ConversationId(11),
        text="bounded",
        allow_tools=True,
        tool_policy=ToolTurnPolicy(max_rounds=2, max_calls=2),
    )

    result = await service.handle_turn(command)

    assert [call.name for call in tools.calls] == ["read_one", "read_two"]
    assert len(model.requests) == 3
    assert result.answer.startswith("Не удалось безопасно")  # noqa: RUF001


@pytest.mark.asyncio
async def test_result_budget_is_checked_before_any_additional_side_effect() -> None:
    repo = FakeRepository()
    model = SequencedModel(
        [
            FakeStream(
                ("<tool>read_one</tool><tool>read_two</tool>",)
            ),
            FakeStream(("safe final",)),
        ]
    )
    tools = FakeTools(output="x" * 256)
    service = ConversationService(
        repo,
        FakeContext(),
        model,
        FakePostTurn(),
        tools=tools,
    )
    command = TurnCommand(
        actor=_actor(),
        surface=ConversationSurface.WEB,
        conversation_id=ConversationId(11),
        text="bounded side effects",
        allow_tools=True,
        tool_policy=ToolTurnPolicy(
            max_rounds=2,
            max_calls=2,
            max_result_chars=256,
            max_total_result_chars=256,
        ),
    )

    result = await service.handle_turn(command)

    assert result.answer == "safe final"
    assert [call.name for call in tools.calls] == ["read_one"]


@pytest.mark.asyncio
async def test_unapproved_tool_is_never_executed() -> None:
    repo = FakeRepository()
    model = SequencedModel(
        [
            FakeStream(("<tool>not_approved</tool>",)),
            FakeStream(("safe final",)),
        ]
    )
    tools = FakeTools()
    service = ConversationService(
        repo,
        FakeContext(),
        model,
        FakePostTurn(),
        tools=tools,
    )

    result = await service.handle_turn(_tool_command(ConversationSurface.TELEGRAM))

    assert tools.calls == []
    assert result.answer == "safe final"
    assert "[error]" in model.requests[1].user


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_intent", "followup", "expected_answer"),
    [
        (
            '<tool>bad-name({"secret":"SECRET_ARG"})</tool>',
            None,
            "Не удалось безопасно завершить операцию",  # noqa: RUF001
        ),
        (
            '<tool>unknown_tool({"secret":"SECRET_ARG"})</tool>',
            "safe final",
            "safe final",
        ),
    ],
)
async def test_shared_service_never_publishes_rejected_raw_tool_markup(
    monkeypatch: pytest.MonkeyPatch,
    raw_intent: str,
    followup: str | None,
    expected_answer: str,
) -> None:
    from app import mcp  # noqa: PLC0415
    from app.adapters.conversation.legacy import (  # noqa: PLC0415
        LegacyConversationTools,
    )

    async def enabled_names() -> list[str]:
        return ["read_file"]

    async def forbidden_call(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("rejected tool must never execute")

    monkeypatch.setattr(mcp, "all_enabled_tool_names", enabled_names)
    monkeypatch.setattr(mcp, "call_tool", forbidden_call)
    streams = [FakeStream((raw_intent,))]
    if followup is not None:
        streams.append(FakeStream((followup,)))
    repository = FakeRepository()
    service = ConversationService(
        repository,
        FakeContext(),
        SequencedModel(streams),
        FakePostTurn(),
        tools=LegacyConversationTools(),
    )

    events = [
        event
        async for event in service.stream_turn(
            _tool_command(ConversationSurface.WEB)
        )
    ]
    result = events[-1].result

    assert result is not None
    assert expected_answer in result.answer
    public = "".join(event.text for event in events)
    persisted = repository.finalized[0][1]
    for secret in ("<tool>", "</tool>", "SECRET_ARG"):
        assert secret not in public
        assert secret not in persisted
    assert "[error] tool is not approved" not in public
    assert "[error] tool is not approved" not in persisted


class BlockingTools(FakeTools):
    def __init__(self, started: asyncio.Event) -> None:
        super().__init__()
        self.started = started
        self.cancelled = asyncio.Event()

    async def execute(
        self,
        command: TurnCommand,
        call: ToolCall,
    ) -> ToolExecution:
        self.calls.append(call)
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cancelled.set()
        raise AssertionError("unreachable")


@pytest.mark.asyncio
async def test_cancellation_during_tool_execution_stops_without_partial_leak() -> None:
    started = asyncio.Event()
    repo = FakeRepository()
    tools = BlockingTools(started)
    service = ConversationService(
        repo,
        FakeContext(),
        SequencedModel([FakeStream(("<tool>read_one</tool>",))]),
        FakePostTurn(),
        tools=tools,
    )

    task = asyncio.create_task(
        service.handle_turn(_tool_command(ConversationSurface.TELEGRAM))
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert tools.cancelled.is_set()
    assert len(repo.appended) == 1
    assert repo.finalized == []


@dataclass
class SharedCancellation:
    cancelled: bool = False
    checks: int = 0

    async def is_cancelled(self, command: TurnCommand) -> bool:
        del command
        self.checks += 1
        return self.cancelled


class CrossWorkerCancellationStream(FakeStream):
    def __init__(self, cancellation: SharedCancellation) -> None:
        super().__init__(())
        self.cancellation = cancellation

    async def _iterate(self) -> AsyncIterator[str]:
        yield "durable before shared stop"
        self.cancellation.cancelled = True
        yield "must not persist"


@pytest.mark.asyncio
async def test_shared_cancellation_is_polled_during_model_stream() -> None:
    cancellation = SharedCancellation()
    repo = FakeRepository()
    service = ConversationService(
        repo,
        FakeContext(),
        FakeModel(CrossWorkerCancellationStream(cancellation)),
        FakePostTurn(),
        cancellation=cancellation,
        cancellation_check_seconds=0,
    )

    with pytest.raises(asyncio.CancelledError):
        await service.handle_turn(_command())

    assert cancellation.checks >= 3
    assert [entry[1] for entry in repo.finalized] == [
        "durable before shared stop"
    ]


class CancellingTools(FakeTools):
    def __init__(self, cancellation: SharedCancellation) -> None:
        super().__init__()
        self.cancellation = cancellation

    async def execute(
        self,
        command: TurnCommand,
        call: ToolCall,
    ) -> ToolExecution:
        execution = await super().execute(command, call)
        self.cancellation.cancelled = True
        return execution


@pytest.mark.asyncio
async def test_shared_cancellation_stops_before_followup_model_call() -> None:
    cancellation = SharedCancellation()
    model = SequencedModel([FakeStream(("<tool>read_one</tool>",))])
    tools = CancellingTools(cancellation)
    service = ConversationService(
        FakeRepository(),
        FakeContext(),
        model,
        FakePostTurn(),
        tools=tools,
        cancellation=cancellation,
        cancellation_check_seconds=0,
    )

    with pytest.raises(asyncio.CancelledError):
        await service.handle_turn(_tool_command(ConversationSurface.WEB))

    assert [call.name for call in tools.calls] == ["read_one"]
    assert len(model.requests) == 1


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("read_file", True),
        ("mcp__server__tool_name", True),
        ("mcp__server__tool-name", False),
        ("mcp__server__tool.name", False),
        ("mcp__server__9tool", False),
        ("mcp__сервер__tool", False),
        ("mcp__server__nested__tool", False),
    ],
)
def test_tool_wire_name_filter_matches_current_parser(
    name: str,
    valid: bool,
) -> None:
    assert is_valid_tool_wire_name(name) is valid
    if valid:
        assert ToolCall(name, {}).name == name
    else:
        with pytest.raises(ValueError, match="invalid tool name"):
            ToolCall(name, {})


@pytest.mark.asyncio
async def test_legacy_tool_adapter_rechecks_registry_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import mcp  # noqa: PLC0415
    from app.adapters.conversation.legacy import (  # noqa: PLC0415
        LegacyConversationTools,
    )

    enabled = [
        "read_file",
        "mcp__server__tool-name",
        "mcp__server__tool.name",
        "mcp__server__9tool",
        "mcp__сервер__tool",
        "mcp__server__nested__tool",
    ]
    calls: list[tuple[str, dict[str, Any], int, int]] = []

    async def enabled_names() -> list[str]:
        return enabled

    async def call_tool(
        name: str,
        arguments: dict[str, Any],
        *,
        user_id: int,
        session_id: int,
    ) -> str:
        calls.append((name, arguments, user_id, session_id))
        return "adapter result"

    monkeypatch.setattr(mcp, "all_enabled_tool_names", enabled_names)
    monkeypatch.setattr(mcp, "call_tool", call_tool)
    adapter = LegacyConversationTools()
    command = _tool_command(ConversationSurface.TELEGRAM)
    parsed = adapter.parse_calls('<tool>read_file({"path":"x"})</tool>')

    assert len(parsed) == 1
    assert parsed[0].arguments == {"path": "x"}
    assert await adapter.approved_tool_names(command) == frozenset({"read_file"})
    execution = await adapter.execute(command, parsed[0])
    assert execution.output == "adapter result"
    assert calls == [("read_file", {"path": "x"}, 7, 11)]

    enabled.clear()
    denied = await adapter.execute(command, parsed[0])
    assert denied.is_error is True
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_owner_turn_gets_web_browse_and_fetch_json_unstripped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PIECE A proof: the owner's own turn must not have web_browse or
    fetch_json stripped by the autonomy risk filter, going through the
    exact same approved_tool_names()/execute() path a real owner turn
    uses -- not by reading tool_policy.py source.
    """
    from app import mcp  # noqa: PLC0415
    from app.adapters.conversation.legacy import (  # noqa: PLC0415
        LegacyConversationTools,
    )

    enabled = ["web_search", "web_browse", "fetch_json"]
    calls: list[tuple[str, dict[str, Any]]] = []

    async def enabled_names() -> list[str]:
        return enabled

    async def call_tool(
        name: str,
        arguments: dict[str, Any],
        *,
        user_id: int,
        session_id: int,
    ) -> str:
        del user_id, session_id
        calls.append((name, arguments))
        return f"{name} ok"

    monkeypatch.setattr(mcp, "all_enabled_tool_names", enabled_names)
    monkeypatch.setattr(mcp, "call_tool", call_tool)
    adapter = LegacyConversationTools()
    command = _tool_command(ConversationSurface.TELEGRAM)
    assert command.actor.is_owner is True

    approved = await adapter.approved_tool_names(command)
    assert approved == frozenset({"web_search", "web_browse", "fetch_json"})

    browse = await adapter.execute(
        command, ToolCall("web_browse", {"url": "https://example.com"})
    )
    fetch = await adapter.execute(
        command, ToolCall("fetch_json", {"url": "https://example.com"})
    )
    assert browse.is_error is False
    assert fetch.is_error is False
    assert calls == [
        ("web_browse", {"url": "https://example.com"}),
        ("fetch_json", {"url": "https://example.com"}),
    ]


@pytest.mark.asyncio
async def test_non_owner_turn_still_strips_web_browse_and_fetch_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The narrowing side of PIECE A: a non-owner (e.g. group) turn keeps
    the pre-existing autonomy stripping, so web_browse/fetch_json never
    reach approval even if the enabled registry includes them.
    """
    from app import mcp  # noqa: PLC0415
    from app.adapters.conversation.legacy import (  # noqa: PLC0415
        LegacyConversationTools,
    )
    from app.domains.chat import ActorContext, ConversationId, TenantId, UserId

    async def enabled_names() -> list[str]:
        return ["web_search", "web_browse", "fetch_json"]

    monkeypatch.setattr(mcp, "all_enabled_tool_names", enabled_names)
    adapter = LegacyConversationTools()
    command = TurnCommand(
        actor=ActorContext(
            tenant_id=TenantId(7),
            user_id=UserId(7),
            is_owner=False,
        ),
        surface=ConversationSurface.TELEGRAM,
        conversation_id=ConversationId(11),
        text="group turn",
        include_private_context=False,
        allow_tools=True,
        tool_policy=ToolTurnPolicy(allowed_tool_names=frozenset({"web_search"})),
    )

    approved = await adapter.approved_tool_names(command)
    assert approved == frozenset({"web_search"})


@pytest.mark.asyncio
async def test_context_advertises_only_executable_tool_wire_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import mcp  # noqa: PLC0415
    from app.adapters.conversation.legacy import (  # noqa: PLC0415
        PersonaContextAdapter,
    )

    advertised: list[str] = []

    async def enabled_names() -> list[str]:
        return [
            "read_file",
            "mcp__safe_server__safe_tool",
            "mcp__unsafe_server__tool-name",
            "mcp__bad__server__tool",
        ]

    def prompt(names: list[str]) -> str:
        advertised.extend(names)
        return "|" + ",".join(names)

    monkeypatch.setattr(mcp, "all_enabled_tool_names", enabled_names)
    monkeypatch.setattr(mcp, "build_tools_prompt", prompt)

    rendered = await PersonaContextAdapter()._tools_context("base")

    assert advertised == ["read_file"]
    assert rendered == "base|read_file"
