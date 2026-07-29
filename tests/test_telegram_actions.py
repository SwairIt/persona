"""Telegram-native action and attachment safety contracts."""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.telegram.actions import (
    immediate_reaction,
    multiple_reactions_requested,
    plan_telegram_actions,
    requested_reaction,
)
from app.integrations.telegram.api import TelegramBotAPI
from app.integrations.telegram.media import (
    TelegramAttachment,
    attachments_from_message,
)
from app.integrations.telegram.worker import _incoming_message


class _Client:
    def __init__(self, result: str) -> None:
        self.result = result
        self.calls = 0

    async def complete(self, request: Any) -> str:
        self.calls += 1
        return self.result


class _RecordingAPI(TelegramBotAPI):
    def __init__(self) -> None:
        super().__init__("test-token")
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 40.0,
    ) -> Any:
        self.calls.append((method, payload or {}))
        if method == "sendMessage":
            return {"message_id": len(self.calls)}
        if method.startswith("send") or method == "copyMessage":
            return {"message_id": 99}
        return True


@pytest.mark.asyncio
async def test_owner_can_select_current_photo_without_exposing_chat_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(
        '{"reaction":"🔥","kind":"photo","media_ref":"attachment:0",'
        '"text":null,"poll_question":null,"poll_options":[]}'
    )
    monkeypatch.setattr(
        "app.integrations.telegram.actions.make_client",
        lambda **_kwargs: client,
    )
    attachment = TelegramAttachment(kind="photo", file_id="bot-local-file")

    plan = await plan_telegram_actions(
        message_text="Отправь это фото обратно",
        answer="Вот оно.",
        attachments=(attachment,),
        is_owner_private=True,
    )

    assert plan.kind == "photo"
    assert plan.media_ref == "attachment:0"
    assert plan.reaction == "🔥"
    assert client.calls == 1


@pytest.mark.asyncio
async def test_group_and_invented_media_reference_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client(
        '{"reaction":"👍","kind":"document",'
        '"media_ref":"https://invented.example/secret.zip"}'
    )
    monkeypatch.setattr(
        "app.integrations.telegram.actions.make_client",
        lambda **_kwargs: client,
    )

    group = await plan_telegram_actions(
        message_text="Отправь документ",
        answer="",
        attachments=(),
        is_owner_private=False,
    )
    owner = await plan_telegram_actions(
        message_text="Отправь документ",
        answer="",
        attachments=(),
        is_owner_private=True,
    )

    assert group.kind == "text"
    assert owner.kind == "text"
    assert group.reaction == owner.reaction == "👍"


@pytest.mark.asyncio
async def test_clear_sentiment_reaction_needs_no_second_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_client(**_kwargs: Any) -> Any:
        raise AssertionError("model should not be called")

    monkeypatch.setattr("app.integrations.telegram.actions.make_client", fail_client)
    plan = await plan_telegram_actions(
        message_text="Спасибо большое!",
        answer="Пожалуйста.",
        attachments=(),
        is_owner_private=True,
    )
    assert plan.reaction == "❤"
    assert plan.kind == "text"


@pytest.mark.asyncio
async def test_explicit_reaction_request_needs_no_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_client(**_kwargs: Any) -> Any:
        raise AssertionError("explicit reaction must be deterministic")

    monkeypatch.setattr("app.integrations.telegram.actions.make_client", fail_client)
    plan = await plan_telegram_actions(
        message_text="Привет, поставь на это сообщение реакцию любую",
        answer="",
        attachments=(),
        is_owner_private=True,
    )

    assert plan.reaction == "👍"
    assert plan.kind == "text"
    assert immediate_reaction("Привет, поставь реакцию любую") == "👍"
    assert requested_reaction("Поставь реакцию 🔥") == "🔥"


def test_reaction_with_another_task_is_not_short_circuited() -> None:
    assert immediate_reaction("Поставь реакцию и расскажи подробнее") is None


def test_typoed_multiple_reaction_request_is_still_fast_pathed() -> None:
    text = "А посмтавь ка несколько сразу, можешь"
    assert immediate_reaction(text) == "👍"
    assert multiple_reactions_requested(text) is True


@pytest.mark.asyncio
async def test_api_native_actions_use_chat_local_payloads() -> None:
    api = _RecordingAPI()

    await api.set_message_reaction(-100, 7, "🔥")
    media_id = await api.send_media(
        "photo",
        -100,
        "file-id",
        caption="caption",
        reply_to_message_id=7,
    )
    poll_id = await api.send_poll(
        -100,
        "Выбор?",
        ("A", "B"),
        reply_to_message_id=7,
    )
    await api.edit_message_text(-100, 99, "new text")
    await api.delete_message(-100, 99)

    assert media_id == poll_id == 99
    reaction = next(payload for method, payload in api.calls if method == "setMessageReaction")
    assert reaction == {
        "chat_id": -100,
        "message_id": 7,
        "reaction": [{"type": "emoji", "emoji": "🔥"}],
    }
    poll = next(payload for method, payload in api.calls if method == "sendPoll")
    assert poll["options"] == [{"text": "A"}, {"text": "B"}]


def test_media_only_and_edited_updates_are_accepted() -> None:
    message = {
        "message_id": 5,
        "from": {"id": 42, "first_name": "Owner"},
        "chat": {"id": 42, "type": "private"},
        "photo": [
            {"file_id": "small", "file_unique_id": "s", "file_size": 10},
            {"file_id": "large", "file_unique_id": "l", "file_size": 20},
        ],
    }
    incoming = _incoming_message({"update_id": 9, "edited_message": message})

    assert incoming is not None
    assert incoming.text.startswith("[Вложение Telegram:")
    assert incoming.attachments[0].file_id == "large"
    assert attachments_from_message(message) == incoming.attachments
