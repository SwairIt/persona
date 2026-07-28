"""SQLite contract test for the legacy conversation persistence adapter."""

from __future__ import annotations

from typing import Any, cast

import pytest

from app.adapters.conversation.legacy import LegacyConversationRepository
from app.application.chat import ModelUsage
from app.chat import create_session, list_messages
from app.domains.chat import ActorContext, ConversationId, TenantId, UserId


async def _user(db: Any, email: str) -> int:
    cursor = await db.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)",
        (email, "test"),
    )
    await db.commit()
    return int(cursor.lastrowid)


@pytest.mark.asyncio
async def test_sqlite_adapter_enforces_tenant_and_persists_stream_lifecycle(db: Any) -> None:
    owner_id = await _user(db, "conversation-owner@example.test")
    other_id = await _user(db, "conversation-other@example.test")
    session = await create_session(owner_id, "Shared Persona")
    session_id = ConversationId(int(session["id"]))
    repository = LegacyConversationRepository()

    owner = ActorContext(TenantId(owner_id), UserId(owner_id), is_owner=True)
    other = ActorContext(TenantId(other_id), UserId(other_id), is_owner=True)
    assert await repository.get(other, session_id) is None
    assert await repository.get(owner, session_id) is not None

    user_message = await repository.append_user(session_id, "hello")
    history = await repository.history(
        session_id,
        max_turns=20,
        exclude_message_id=user_message.id,
    )
    assert history == ()

    assistant_id = await repository.begin_assistant(session_id, provider="fake")
    await repository.update_assistant(assistant_id, "partial")
    usage = ModelUsage(
        provider="fake",
        model="fake-model",
        input_tokens=4,
        output_tokens=2,
    )
    await repository.finalize_assistant(
        assistant_id,
        "complete",
        elapsed_ms=25,
        usage=usage,
    )

    messages = await list_messages(int(session_id))
    assert [(message["role"], message["content"]) for message in messages] == [
        ("user", "hello"),
        ("assistant", "complete"),
    ]
    last_message = cast("dict[str, Any]", messages[-1])
    assert last_message["is_streaming"] is False
    assert messages[-1]["model_used"] == "fake"
    assert messages[-1]["elapsed_ms"] == 25
    assert messages[-1]["input_tokens"] == 4
    assert messages[-1]["output_tokens"] == 2
