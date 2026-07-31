"""A ``research`` thought chain: seeded from a real request (no model call),
allowed exactly two read-only tools between steps, and its honesty prompts.
"""
# ruff: noqa: RUF001

from __future__ import annotations

from typing import Any

import pytest

from app.thinking.loop import (
    _RESEARCH_CONCLUSION_SYSTEM,
    _RESEARCH_STEP_SYSTEM,
    _call_research_tool,
    advance_chain,
    seed_research_chain,
)
from app.thinking.research_tools import RESEARCH_TOOLS, is_research_tool_allowed
from app.thinking.settings import ALL_SEED_KINDS, ThinkingSettings
from app.thinking.store import ThoughtStore


class FakeClient:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.requests: list[Any] = []

    async def complete(self, request: Any) -> str:
        self.requests.append(request)
        return self.replies.pop(0) if self.replies else ""


async def _user(db, user_id: int = 7) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(?,?,?)",
        (user_id, f"{user_id}@example.test", "x"),
    )
    await db.commit()


def _settings(**over: Any) -> ThinkingSettings:
    base = dict(
        enabled=True, cap_mode="fixed", step_cap=5, emergency_cap=50,
        daily_budget=60, seed_kinds=ALL_SEED_KINDS, may_write_to_chat=False,
    )
    base.update(over)
    return ThinkingSettings(**base)


async def test_seed_research_chain_needs_no_model_call(db) -> None:
    """The topic is real (it came from an actual chat message), not model-
    invented, so seeding never calls the LLM at all."""
    await _user(db)
    store = ThoughtStore()
    chain_id = await seed_research_chain(
        store,
        persona_user_id=7,
        topic="Лабиринт Фавна",
        chat_id=-100500,
        source_scope="group",
    )
    assert chain_id is not None
    steps = await store.chain_steps(chain_id)
    assert steps[0]["kind"] == "seed"
    assert steps[0]["seed_kind"] == "research"
    assert steps[0]["text"] == "Лабиринт Фавна"
    assert steps[0]["certainty"] == "observation"

    chain = await store.get_chain(chain_id)
    assert chain["source_chat_id"] == -100500
    assert chain["source_scope"] == "group"


async def test_research_chain_calls_web_search_and_result_reaches_next_step(
    db, monkeypatch
) -> None:
    """The whole mechanism under test: a research chain calls web_search
    (mocked here) and the retrieved text must reach the NEXT step's model
    request — not just exist somewhere unused."""
    await _user(db)
    store = ThoughtStore()
    chain_id = await seed_research_chain(
        store, persona_user_id=7, topic="Лабиринт Фавна",
        chat_id=-100500, source_scope="group",
    )

    marker = "ОСОБАЯ-ФРАЗА-ИЗ-ПОИСКА-ДЛЯ-ТЕСТА"
    search_calls: list[dict[str, Any]] = []

    async def fake_web_search(args: dict[str, Any], user_id: int = 0) -> str:
        search_calls.append(args)
        return f"[ok] поиск «{args.get('query')}»:\n- Рецензия: {marker}"

    monkeypatch.setattr("app.mcp.builtin_tools.web_search", fake_web_search)

    settings = _settings(step_cap=5)

    # First advance: gathers evidence via web_search, no model call yet.
    client = FakeClient(["не должно быть вызвано"])
    outcome = await advance_chain(store, settings, chain_id=chain_id, client=client)
    assert outcome == "stepped"
    assert client.requests == []
    assert search_calls and search_calls[0]["query"] == "Лабиринт Фавна"

    steps = await store.chain_steps(chain_id)
    assert marker in steps[-1]["text"]

    # Second advance: a normal step, and the search text must be part of
    # the history handed to the model this time.
    client2 = FakeClient(["НАБЛЮДЕНИЕ: по прочитанному это тёмная сказка"])
    outcome2 = await advance_chain(store, settings, chain_id=chain_id, client=client2)
    assert outcome2 == "stepped"
    assert len(client2.requests) == 1
    assert marker in client2.requests[0].user
    # The research chain must use the honesty-augmented system prompt.
    assert client2.requests[0].system == _RESEARCH_STEP_SYSTEM


async def test_research_conclusion_uses_the_honesty_prompt(db, monkeypatch) -> None:
    await _user(db)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="Лабиринт Фавна", seed_kind="research",
        source_scope="group", source_session_id=None, certainty="observation",
        source_chat_id=-100500,
    )

    async def fake_web_search(args: dict[str, Any], user_id: int = 0) -> str:
        return "[ok] нашла кое-что"

    monkeypatch.setattr("app.mcp.builtin_tools.web_search", fake_web_search)

    settings = _settings(step_cap=1)
    # Step 1: automatic search step (consumes the only allowed step).
    await advance_chain(store, settings, chain_id=chain_id, client=FakeClient([]))
    # Step 2: cap reached -> forced conclusion.
    client = FakeClient(["НАБЛЮДЕНИЕ: по прочитанному это мрачная сказка"])
    outcome = await advance_chain(store, settings, chain_id=chain_id, client=client)
    assert outcome == "closed"
    assert client.requests[0].system == _RESEARCH_CONCLUSION_SYSTEM


def test_research_tools_allowlist_excludes_writing_and_fetch_json() -> None:
    assert RESEARCH_TOOLS == frozenset({"web_search", "web_browse"})
    assert is_research_tool_allowed("web_search")
    assert is_research_tool_allowed("web_browse")
    for forbidden in ("fetch_json", "run_shell", "run_mac", "write_file", "delete_path"):
        assert not is_research_tool_allowed(forbidden)


async def test_research_chain_cannot_call_fetch_json_or_run_shell() -> None:
    for forbidden in ("fetch_json", "run_shell"):
        with pytest.raises(PermissionError):
            await _call_research_tool(forbidden, {})


def test_research_prompts_state_persona_only_read_never_watched() -> None:
    honesty_markers = ("прочита", "не смотрела")
    for prompt in (_RESEARCH_STEP_SYSTEM, _RESEARCH_CONCLUSION_SYSTEM):
        assert any(marker in prompt for marker in honesty_markers), (
            "research prompt must instruct the model that it only READ "
            "about the topic, and never claim to have watched/experienced it"
        )
