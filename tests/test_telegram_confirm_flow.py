"""Task 3: the Telegram confirm/cancel button for execution-class tools.

The property under test: a confirmation callback executes the arguments
stored in the parked DB row, never the arguments carried in the callback
payload -- the payload carries only the opaque pending id (see
``_ConfirmingTelegramTools`` and ``TelegramWorker._handle_callback_query``
in ``app/integrations/telegram/worker.py``). A second property under test:
an execution-class request from a GROUP is refused outright by the
existing tool-policy narrowing (Task 1) before it ever reaches the
confirmation wrapper, so it parks nothing regardless of who sent it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.adapters.conversation.legacy import LegacyConversationTools
from app.application.chat import ConversationService, ToolCall, ToolTurnPolicy, TurnCommand
from app.domains.chat import ActorContext, ConversationId, ConversationSurface, TenantId, UserId
from app.integrations.telegram.config import TelegramConfig
from app.integrations.telegram.pending_actions import PendingActionStore
from app.integrations.telegram.repository import TelegramBinding
from app.integrations.telegram.worker import (
    _ConfirmingTelegramTools,
    TelegramWorker,
)
from tests.test_conversation_service import (
    FakeContext,
    FakePostTurn,
    FakeRepository,
    FakeStream,
    SequencedModel,
    _actor,
)


class FakeConfirmAPI:
    """Minimal TelegramBotAPI stand-in: only the surface confirm-flow needs."""

    def __init__(self) -> None:
        self.buttons_sent: list[tuple[int, str, list[tuple[str, str]]]] = []
        self.sent: list[tuple[int, str]] = []
        self.answers: list[tuple[str, str, bool]] = []

    async def send_message_with_buttons(
        self,
        chat_id: int,
        text: str,
        buttons: list[tuple[str, str]],
        *,
        reply_to_message_id: int | None = None,
    ) -> int | None:
        del reply_to_message_id
        self.buttons_sent.append((chat_id, text, buttons))
        return 1

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> tuple[int, ...]:
        del reply_to_message_id
        self.sent.append((chat_id, text))
        return (1,)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str = "",
        show_alert: bool = False,
    ) -> None:
        self.answers.append((callback_query_id, text, show_alert))


class FakeCallbackRepository:
    def __init__(self, binding: TelegramBinding | None) -> None:
        self.binding = binding

    async def get_binding(self) -> TelegramBinding | None:
        return self.binding


def _telegram_command(
    *,
    is_owner: bool,
    is_group: bool,
    chat_id: int,
) -> TurnCommand:
    policy = ToolTurnPolicy(allowed_tool_names=frozenset({"web_search"})) if is_group else None
    return TurnCommand(
        actor=ActorContext(
            tenant_id=TenantId(7),
            user_id=UserId(7),
            is_owner=is_owner,
        ),
        surface=ConversationSurface.TELEGRAM,
        conversation_id=ConversationId(11),
        text="do it",
        include_private_context=not is_group,
        allow_tools=True,
        tool_policy=policy,
        metadata={"telegram_chat_id": str(chat_id)},
    )


def _worker(binding: TelegramBinding | None, pending: PendingActionStore) -> TelegramWorker:
    api = FakeConfirmAPI()
    from tests.test_telegram_integration import FakeService  # noqa: PLC0415

    worker = TelegramWorker(
        TelegramConfig(bot_token="not-a-real-token"),
        api=api,  # type: ignore[arg-type]
        repository=FakeCallbackRepository(binding),  # type: ignore[arg-type]
        service=FakeService(),  # type: ignore[arg-type]
        pending_actions=pending,
    )
    return worker


@pytest.mark.asyncio
async def test_execution_class_call_parks_and_sends_card_without_executing(
    monkeypatch: pytest.MonkeyPatch,
    db: Any,
) -> None:
    del db
    from app import mcp  # noqa: PLC0415

    async def enabled_names() -> list[str]:
        return ["run_shell"]

    async def forbidden_call(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("execution-class tool must never run inline")

    monkeypatch.setattr(mcp, "all_enabled_tool_names", enabled_names)
    monkeypatch.setattr(mcp, "call_tool", forbidden_call)

    api = FakeConfirmAPI()
    pending = PendingActionStore()
    tools = _ConfirmingTelegramTools(api, pending, LegacyConversationTools())  # type: ignore[arg-type]
    command = _telegram_command(is_owner=True, is_group=False, chat_id=555)
    call = ToolCall("run_shell", {"command": "rm -rf /"})

    execution = await tools.execute(command, call)

    assert execution.is_error is True
    assert "подтверждени" in execution.output.lower()
    assert len(api.buttons_sent) == 1
    chat_id, text, buttons = api.buttons_sent[0]
    assert chat_id == 555
    assert "run_shell" in text
    assert len(buttons) == 2
    # The callback_data carries ONLY the pending id -- never the tool name
    # or its arguments.
    for _label, data in buttons:
        assert data.rsplit(":", 1)[-1].isdigit()
        assert "run_shell" not in data
        assert "rm -rf" not in data


@pytest.mark.asyncio
async def test_confirm_executes_parked_args_not_payload_args(
    monkeypatch: pytest.MonkeyPatch,
    db: Any,
) -> None:
    """The core property: execution uses the DB row's arguments, never
    anything that could be smuggled through callback_data (which here
    structurally cannot carry arguments at all -- only the pending id)."""
    del db
    from app import mcp  # noqa: PLC0415

    calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(name: str, args: dict[str, Any], **_kwargs: Any) -> str:
        calls.append((name, args))
        return "done"

    monkeypatch.setattr(mcp, "call_tool", call_tool)

    binding = TelegramBinding(telegram_user_id=1, persona_user_id=7)
    pending = PendingActionStore()
    parked_args = {"command": "echo parked-args-only"}
    pending_id = await pending.park(
        7, tool_name="run_shell", args=parked_args, chat_id=555
    )
    worker = _worker(binding, pending)

    await worker._handle_callback_query(
        {"id": "cb-1", "data": f"persona_confirm:{pending_id}", "from": {"id": 1}}
    )

    assert calls == [("run_shell", parked_args)]
    assert any("Выполнено" in text for _cid, text in worker.api.sent)


@pytest.mark.asyncio
async def test_second_confirm_press_does_not_execute_again(
    monkeypatch: pytest.MonkeyPatch,
    db: Any,
) -> None:
    del db
    from app import mcp  # noqa: PLC0415

    calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(name: str, args: dict[str, Any], **_kwargs: Any) -> str:
        calls.append((name, args))
        return "done"

    monkeypatch.setattr(mcp, "call_tool", call_tool)

    binding = TelegramBinding(telegram_user_id=1, persona_user_id=7)
    pending = PendingActionStore()
    pending_id = await pending.park(
        7, tool_name="run_shell", args={"command": "echo hi"}, chat_id=555
    )
    worker = _worker(binding, pending)
    callback = {"id": "cb-1", "data": f"persona_confirm:{pending_id}", "from": {"id": 1}}

    await worker._handle_callback_query(callback)
    await worker._handle_callback_query(callback)

    assert len(calls) == 1
    assert any("истёк" in text or "использ" in text for _cid, text, _alert in worker.api.answers)


@pytest.mark.asyncio
async def test_callback_from_non_owner_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    db: Any,
) -> None:
    del db
    from app import mcp  # noqa: PLC0415

    calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(name: str, args: dict[str, Any], **_kwargs: Any) -> str:
        calls.append((name, args))
        return "done"

    monkeypatch.setattr(mcp, "call_tool", call_tool)

    binding = TelegramBinding(telegram_user_id=1, persona_user_id=7)
    pending = PendingActionStore()
    pending_id = await pending.park(
        7, tool_name="run_shell", args={"command": "echo hi"}, chat_id=555
    )
    worker = _worker(binding, pending)

    # Sender id 999 is not the bound owner (1).
    await worker._handle_callback_query(
        {"id": "cb-1", "data": f"persona_confirm:{pending_id}", "from": {"id": 999}}
    )

    assert calls == []
    # The row is untouched (still unclaimed) -- the real owner can still
    # confirm it afterwards.
    claimed = await pending.claim(7, pending_id)
    assert claimed is not None
    assert claimed["args"] == {"command": "echo hi"}


@pytest.mark.asyncio
async def test_cancel_consumes_without_executing(
    monkeypatch: pytest.MonkeyPatch,
    db: Any,
) -> None:
    del db
    from app import mcp  # noqa: PLC0415

    async def forbidden_call(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("cancel must never execute the tool")

    monkeypatch.setattr(mcp, "call_tool", forbidden_call)

    binding = TelegramBinding(telegram_user_id=1, persona_user_id=7)
    pending = PendingActionStore()
    pending_id = await pending.park(
        7, tool_name="run_shell", args={"command": "echo hi"}, chat_id=555
    )
    worker = _worker(binding, pending)

    await worker._handle_callback_query(
        {"id": "cb-1", "data": f"persona_cancel:{pending_id}", "from": {"id": 1}}
    )

    # Consumed by the cancel -- a later confirm press cannot resurrect it.
    claimed = await pending.claim(7, pending_id)
    assert claimed is None


@pytest.mark.parametrize("is_owner", [True, False])
@pytest.mark.asyncio
async def test_group_execution_request_is_refused_outright_and_parks_nothing(
    monkeypatch: pytest.MonkeyPatch,
    is_owner: bool,
) -> None:
    from app import mcp  # noqa: PLC0415

    async def enabled_names() -> list[str]:
        return ["run_shell", "web_search"]

    async def forbidden_call(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("group turn must never execute run_shell")

    monkeypatch.setattr(mcp, "all_enabled_tool_names", enabled_names)
    monkeypatch.setattr(mcp, "call_tool", forbidden_call)

    api = FakeConfirmAPI()

    class NeverParks:
        async def park(self, *_args: Any, **_kwargs: Any) -> int:
            raise AssertionError("group turn must never park an execution action")

    tools = _ConfirmingTelegramTools(api, NeverParks(), LegacyConversationTools())  # type: ignore[arg-type]
    command = _telegram_command(is_owner=is_owner, is_group=True, chat_id=-100)
    streams = [
        FakeStream(('<tool>run_shell({"command": "ls"})</tool>',)),
        FakeStream(("Не могу выполнить это действие в группе.",)),
    ]
    repo = FakeRepository()
    service = ConversationService(
        repo,
        FakeContext(),
        SequencedModel(streams),
        FakePostTurn(),
        tools=tools,
    )

    result = await service.handle_turn(command)

    assert api.buttons_sent == []
    assert "Не могу выполнить" in result.answer
    persisted = repo.finalized[0][1]
    assert "run_shell" not in persisted
    assert "ожидание" not in persisted
