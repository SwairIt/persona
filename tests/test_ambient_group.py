"""Application and adapter tests for reactive ambient Telegram groups."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.application.ambient_group import (
    AmbientGroupService,
    AmbientGroupTurn,
)


def _turn(message_id: int = 1) -> AmbientGroupTurn:
    return AmbientGroupTurn(
        tenant_id=7,
        conversation_id=11,
        external_chat_id=-100,
        update_id=message_id,
        message_id=message_id,
        text="обычное сообщение без упоминания",
        sender_label="Участник",
        chat_title="Команда",
    )


@dataclass
class FakeClock:
    value: float = 100.0

    def now(self) -> float:
        return self.value


@dataclass
class FakeDecision:
    answers: list[bool]
    calls: list[AmbientGroupTurn] = field(default_factory=list)
    raw_metadata: str = "SECRET_DECISION_REASON"

    async def should_reply(self, turn: AmbientGroupTurn) -> bool:
        self.calls.append(turn)
        return self.answers.pop(0)


@dataclass
class FakeTurns:
    answer: str = "ambient answer"
    persisted: list[AmbientGroupTurn] = field(default_factory=list)
    replied: list[AmbientGroupTurn] = field(default_factory=list)

    async def persist(self, turn: AmbientGroupTurn) -> None:
        self.persisted.append(turn)

    async def reply(self, turn: AmbientGroupTurn) -> str:
        self.replied.append(turn)
        return self.answer


@pytest.mark.asyncio
async def test_silent_decision_persists_every_message_without_metadata_leak() -> None:
    decision = FakeDecision([False])
    turns = FakeTurns()
    service = AmbientGroupService(decision, turns)

    outcome = await service.handle(_turn())

    assert not outcome.should_send
    assert outcome.reply == ""
    assert turns.persisted == [_turn()]
    assert turns.replied == []
    assert decision.raw_metadata not in outcome.reply


@pytest.mark.asyncio
async def test_reply_decision_owns_exactly_one_persistence_path() -> None:
    decision = FakeDecision([True])
    turns = FakeTurns()
    service = AmbientGroupService(decision, turns)

    outcome = await service.handle(_turn())

    assert outcome.reply == "ambient answer"
    assert turns.persisted == []
    assert turns.replied == [_turn()]


@pytest.mark.asyncio
async def test_cooldown_and_decision_rate_limit_suppress_group_spam() -> None:
    clock = FakeClock()
    decision = FakeDecision([True, False])
    turns = FakeTurns()
    service = AmbientGroupService(
        decision,
        turns,
        clock=clock,
        decision_interval_seconds=2,
        reply_cooldown_seconds=30,
    )

    first = await service.handle(_turn(1))
    clock.value += 1
    second = await service.handle(_turn(2))
    clock.value += 31
    third = await service.handle(_turn(3))

    assert first.should_send
    assert not second.should_send
    assert not third.should_send
    assert decision.calls == [_turn(1), _turn(3)]
    assert turns.replied == [_turn(1)]
    assert turns.persisted == [_turn(2), _turn(3)]


@pytest.mark.asyncio
async def test_decision_failure_is_silent_and_still_persists() -> None:
    class OfflineDecision:
        async def should_reply(self, turn: AmbientGroupTurn) -> bool:
            del turn
            raise RuntimeError("LLM offline with SECRET_METADATA")

    turns = FakeTurns()
    outcome = await AmbientGroupService(OfflineDecision(), turns).handle(_turn())

    assert outcome.reply == ""
    assert turns.persisted == [_turn()]


@pytest.mark.asyncio
async def test_reply_failure_is_silent_after_reply_port_owns_turn() -> None:
    class OfflineReplyTurns(FakeTurns):
        async def reply(self, turn: AmbientGroupTurn) -> str:
            self.replied.append(turn)
            raise RuntimeError("LLM offline with SECRET_METADATA")

    turns = OfflineReplyTurns()
    outcome = await AmbientGroupService(FakeDecision([True]), turns).handle(_turn())

    assert outcome.reply == ""
    assert turns.replied == [_turn()]
    assert turns.persisted == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_decision", "expected"),
    [
        ("REPLY", True),
        ("SILENT", False),
        ("REPLY\nSECRET_DECISION_REASON", False),
    ],
)
async def test_decision_adapter_is_bounded_and_rejects_raw_metadata(
    monkeypatch: pytest.MonkeyPatch,
    raw_decision: str,
    expected: bool,
) -> None:
    from app.integrations.telegram import ambient  # noqa: PLC0415

    async def session(_tenant_id: int, _conversation_id: int) -> dict[str, int]:
        return {"id": 11, "user_id": 7}

    async def history(
        _conversation_id: int,
        *,
        max_turns: int,
    ) -> list[dict[str, str]]:
        assert max_turns == 28
        return [{"role": "user", "content": "GROUP_ONLY"}]

    requests: list[Any] = []

    class FakeClient:
        async def complete(self, request: Any) -> str:
            requests.append(request)
            return raw_decision

    monkeypatch.setattr(ambient, "get_session", session)
    monkeypatch.setattr(ambient, "build_history_for_llm", history)
    monkeypatch.setattr(ambient, "make_client", lambda **_kwargs: FakeClient())

    result = await ambient.TelegramAmbientDecisionAdapter().should_reply(_turn())

    assert result is expected
    assert len(requests) == 1
    assert requests[0].max_tokens == 8
    assert requests[0].temperature == 0.0
    assert "GROUP_ONLY" in requests[0].user
    assert "SECRET_DECISION_REASON" not in requests[0].user


@pytest.mark.asyncio
async def test_other_agent_addressee_is_silent_without_calling_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.integrations.telegram import ambient  # noqa: PLC0415

    monkeypatch.setattr(
        ambient,
        "make_client",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("LLM must not run")),
    )
    turn = AmbientGroupTurn(
        tenant_id=7,
        conversation_id=11,
        external_chat_id=-100,
        update_id=9,
        message_id=9,
        text="Инди, привет",
        sender_label="Олег",
        chat_title="Команда",
    )

    assert not await ambient.TelegramAmbientDecisionAdapter().should_reply(turn)


@pytest.mark.asyncio
async def test_newest_owner_rule_can_override_other_agent_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.integrations.telegram import ambient  # noqa: PLC0415

    class Rules:
        async def group_behavior_rules(self, _chat_id: int) -> tuple[str, ...]:
            return (
                "не отвечай, когда обращаются к Инди",
                "отвечай, когда обращаются к Инди",
            )

    monkeypatch.setattr(
        ambient,
        "make_client",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("LLM must not run")),
    )
    turn = AmbientGroupTurn(
        tenant_id=7,
        conversation_id=11,
        external_chat_id=-100,
        update_id=10,
        message_id=10,
        text="Инди, привет",
        sender_label="Олег",
        chat_title="Команда",
    )

    assert await ambient.TelegramAmbientDecisionAdapter(Rules()).should_reply(turn)


@pytest.mark.asyncio
async def test_group_reply_adapter_uses_only_group_history_and_persists_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.integrations.telegram import ambient  # noqa: PLC0415

    async def session(_tenant_id: int, _conversation_id: int) -> dict[str, Any]:
        return {"id": 11, "user_id": 7, "model": "group-model"}

    async def history(
        _conversation_id: int,
        *,
        max_turns: int,
    ) -> list[dict[str, str]]:
        assert max_turns == 32
        return [
            {"role": "user", "content": "GROUP_HISTORY_ONLY"},
            {"role": "assistant", "content": "prior group reply"},
        ]

    appended: list[tuple[str, str]] = []

    async def append(
        _conversation_id: int,
        role: str,
        content: str,
        **_kwargs: Any,
    ) -> dict[str, int]:
        appended.append((role, content))
        return {"id": len(appended)}

    requests: list[Any] = []

    class FakeClient:
        provider = "fake"

        def __init__(self) -> None:
            self._inner = self
            self._model = "initial"

        async def complete(self, request: Any) -> str:
            requests.append(request)
            return "Короткий ответ группе"

    fake = FakeClient()
    monkeypatch.setattr(ambient, "get_session", session)
    monkeypatch.setattr(ambient, "build_history_for_llm", history)
    monkeypatch.setattr(ambient, "append_message", append)
    monkeypatch.setattr(ambient, "touch_session", lambda *_a: _async_none())
    monkeypatch.setattr(ambient, "make_client", lambda **_kwargs: fake)
    monkeypatch.setattr(
        ambient,
        "get_active_system_prompt",
        lambda: _async_text("PERSONA_STYLE"),
    )

    answer = await ambient.TelegramAmbientTurnAdapter().reply(_turn())

    assert answer == "Короткий ответ группе"
    assert [role for role, _content in appended] == ["user", "assistant"]
    assert sum("обычное сообщение" in content for _role, content in appended) == 1
    assert fake._model == "group-model"
    assert len(requests) == 1
    request = requests[0]
    assert "GROUP_HISTORY_ONLY" in request.user
    assert "PRIVATE_OWNER_SECRET" not in request.system + request.user
    assert request.max_tokens == 900
    assert request.temperature == 0.55


async def _async_none() -> None:
    return None


async def _async_text(value: str) -> str:
    return value
