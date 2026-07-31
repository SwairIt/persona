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


async def test_research_chain_with_no_search_results_closes_honestly_without_speculation(
    db, monkeypatch
) -> None:
    """The exact defect this guard exists for: asked to look up 'лабиринт
    фавна' (Pan's Labyrinth), the search came back empty and the model
    invented a "metaphorical" answer instead of admitting it found nothing.
    An empty search must close the chain right there — zero reasoning steps
    between the search and the conclusion, no model call, no speculation."""
    await _user(db)
    store = ThoughtStore()
    chain_id = await seed_research_chain(
        store, persona_user_id=7, topic="лабиринт фавна",
        chat_id=-100500, source_scope="group",
    )

    async def fake_web_search(args: dict[str, Any], user_id: int = 0) -> str:
        return "[ok] ничего не найдено: лабиринт фавна"

    monkeypatch.setattr("app.mcp.builtin_tools.web_search", fake_web_search)

    settings = _settings(step_cap=5)
    client = FakeClient(["не должно быть вызвано никогда"])
    outcome = await advance_chain(store, settings, chain_id=chain_id, client=client)

    assert outcome == "closed"
    assert client.requests == []  # the model was never even consulted

    steps = await store.chain_steps(chain_id)
    kinds = [s["kind"] for s in steps]
    assert kinds == ["seed", "step", "conclusion"], (
        "no reasoning steps may sit between the empty search and the conclusion"
    )
    assert "не нашла информации" in steps[-1]["text"]

    chain = await store.get_chain(chain_id)
    assert chain["status"] == "closed"


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


def _model_settings(**over: Any) -> ThinkingSettings:
    return _settings(cap_mode="model", emergency_cap=50, **over)


async def test_research_chain_ishu_triggers_second_search_and_reaches_next_step(
    db, monkeypatch
) -> None:
    """Owner mandate: she decides for herself whether to look further. An
    ``ИЩУ: <query>`` reply must run a real (mocked) web_search and the
    result must reach the NEXT step's model request."""
    await _user(db)
    store = ThoughtStore()
    chain_id = await seed_research_chain(
        store, persona_user_id=7, topic="гильермо дель торо",
        chat_id=-100500, source_scope="group",
    )

    async def fake_web_search(args: dict[str, Any], user_id: int = 0) -> str:
        query = args.get("query", "")
        return f"[ok] поиск «{query}»:\n- https://example.test/bio\n  биография"

    monkeypatch.setattr("app.mcp.builtin_tools.web_search", fake_web_search)
    settings = _model_settings()

    # Step 1: automatic first search (deterministic, no model call).
    await advance_chain(store, settings, chain_id=chain_id, client=FakeClient([]))

    # Step 2: model asks to search again with a refined query.
    client = FakeClient(["ИЩУ: гильермо дель торо фильмография"])
    outcome = await advance_chain(store, settings, chain_id=chain_id, client=client)
    assert outcome == "stepped"

    steps = await store.chain_steps(chain_id)
    assert "гильермо дель торо фильмография" in steps[-1]["text"]
    assert "биография" in steps[-1]["text"]

    # Step 3: the new search results must reach the next model call.
    client2 = FakeClient(["НАБЛЮДЕНИЕ: по прочитанному это известный режиссёр"])
    outcome2 = await advance_chain(store, settings, chain_id=chain_id, client=client2)
    assert outcome2 == "stepped"
    assert "фильмография" in client2.requests[0].user


async def test_research_chain_otkryvayu_known_url_fetches_it(db, monkeypatch) -> None:
    """``ОТКРЫВАЮ: <url>`` for a URL that appeared in an earlier search
    result of THIS chain must fetch it (web_browse, mocked)."""
    await _user(db)
    store = ThoughtStore()
    chain_id = await seed_research_chain(
        store, persona_user_id=7, topic="Лабиринт Фавна",
        chat_id=-100500, source_scope="group",
    )
    url = "https://example.test/labyrinth"

    async def fake_web_search(args: dict[str, Any], user_id: int = 0) -> str:
        return f"[ok] поиск «{args.get('query')}»:\n- {url}\n  рецензия"

    browse_calls: list[dict[str, Any]] = []

    async def fake_web_browse(args: dict[str, Any], user_id: int = 0) -> str:
        browse_calls.append(args)
        return "[ok] содержимое страницы про Лабиринт Фавна"

    monkeypatch.setattr("app.mcp.builtin_tools.web_search", fake_web_search)
    monkeypatch.setattr("app.mcp.builtin_tools.web_browse", fake_web_browse)
    settings = _model_settings()

    await advance_chain(store, settings, chain_id=chain_id, client=FakeClient([]))

    client = FakeClient([f"ОТКРЫВАЮ: {url}"])
    outcome = await advance_chain(store, settings, chain_id=chain_id, client=client)
    assert outcome == "stepped"
    assert browse_calls and browse_calls[0]["url"] == url

    steps = await store.chain_steps(chain_id)
    assert "содержимое страницы" in steps[-1]["text"]


async def test_research_chain_otkryvayu_unseen_url_is_refused(db, monkeypatch) -> None:
    """A URL that never appeared in this chain's own search results must be
    refused — never fetched — otherwise the model can invent URLs."""
    await _user(db)
    store = ThoughtStore()
    chain_id = await seed_research_chain(
        store, persona_user_id=7, topic="Лабиринт Фавна",
        chat_id=-100500, source_scope="group",
    )

    async def fake_web_search(args: dict[str, Any], user_id: int = 0) -> str:
        return "[ok] поиск «...»:\n- https://example.test/real\n  рецензия"

    browse_calls: list[dict[str, Any]] = []

    async def fake_web_browse(args: dict[str, Any], user_id: int = 0) -> str:
        browse_calls.append(args)
        return "[ok] should never happen"

    monkeypatch.setattr("app.mcp.builtin_tools.web_search", fake_web_search)
    monkeypatch.setattr("app.mcp.builtin_tools.web_browse", fake_web_browse)
    settings = _model_settings()

    await advance_chain(store, settings, chain_id=chain_id, client=FakeClient([]))

    invented_url = "https://invented.test/does-not-exist"
    client = FakeClient([f"ОТКРЫВАЮ: {invented_url}"])
    outcome = await advance_chain(store, settings, chain_id=chain_id, client=client)
    assert outcome == "stepped"
    assert browse_calls == []  # never fetched

    steps = await store.chain_steps(chain_id)
    assert "не встречалась" in steps[-1]["text"]
    assert invented_url in steps[-1]["text"]


async def test_research_chain_sixth_search_is_refused_and_told_to_conclude(
    db, monkeypatch
) -> None:
    """At most 5 searches per chain (the deterministic first one included).
    A 6th ИЩУ must be refused in code, without calling web_search again."""
    await _user(db)
    store = ThoughtStore()
    chain_id = await seed_research_chain(
        store, persona_user_id=7, topic="тема", chat_id=-100500, source_scope="group",
    )

    search_calls: list[str] = []

    async def fake_web_search(args: dict[str, Any], user_id: int = 0) -> str:
        search_calls.append(args.get("query", ""))
        return f"[ok] поиск «{args.get('query')}»:\n- https://example.test/{len(search_calls)}\n  X"

    monkeypatch.setattr("app.mcp.builtin_tools.web_search", fake_web_search)
    settings = _model_settings()

    # Step 1: automatic first search (search #1).
    await advance_chain(store, settings, chain_id=chain_id, client=FakeClient([]))
    # Steps 2-5: four more distinct ИЩУ searches (#2-#5).
    for i in range(4):
        client = FakeClient([f"ИЩУ: запрос номер {i}"])
        outcome = await advance_chain(store, settings, chain_id=chain_id, client=client)
        assert outcome == "stepped"
    assert len(search_calls) == 5

    # 6th search attempt: must be refused, web_search must not be called again.
    client = FakeClient(["ИЩУ: запрос номер 5"])
    outcome = await advance_chain(store, settings, chain_id=chain_id, client=client)
    assert outcome == "stepped"
    assert len(search_calls) == 5  # unchanged — refused before calling the tool

    steps = await store.chain_steps(chain_id)
    assert "лимит поисков" in steps[-1]["text"]
    assert "закончить" in steps[-1]["text"]


async def test_research_chain_repeated_query_does_not_spend_lookup_or_refetch(
    db, monkeypatch
) -> None:
    """Repeating an identical query must not spend a lookup and must not
    re-fetch — she is told it was already tried instead."""
    await _user(db)
    store = ThoughtStore()
    chain_id = await seed_research_chain(
        store, persona_user_id=7, topic="гильермо дель торо",
        chat_id=-100500, source_scope="group",
    )

    search_calls: list[str] = []

    async def fake_web_search(args: dict[str, Any], user_id: int = 0) -> str:
        search_calls.append(args.get("query", ""))
        return f"[ok] поиск «{args.get('query')}»:\n- https://example.test/x\n  X"

    monkeypatch.setattr("app.mcp.builtin_tools.web_search", fake_web_search)
    settings = _model_settings()

    await advance_chain(store, settings, chain_id=chain_id, client=FakeClient([]))
    assert search_calls == ["гильермо дель торо"]

    # Repeat the exact same query the automatic first search already used.
    client = FakeClient(["ИЩУ: гильермо дель торо"])
    outcome = await advance_chain(store, settings, chain_id=chain_id, client=client)
    assert outcome == "stepped"
    assert search_calls == ["гильермо дель торо"]  # not called again

    steps = await store.chain_steps(chain_id)
    assert "уже искала" in steps[-1]["text"]


async def test_research_chain_hvatit_still_closes_the_chain(db, monkeypatch) -> None:
    await _user(db)
    store = ThoughtStore()
    chain_id = await seed_research_chain(
        store, persona_user_id=7, topic="тема", chat_id=-100500, source_scope="group",
    )

    async def fake_web_search(args: dict[str, Any], user_id: int = 0) -> str:
        return "[ok] нашла кое-что полезное"

    monkeypatch.setattr("app.mcp.builtin_tools.web_search", fake_web_search)
    settings = _model_settings()

    await advance_chain(store, settings, chain_id=chain_id, client=FakeClient([]))

    client = FakeClient(["ХВАТИТ: данных достаточно, вот вывод"])
    outcome = await advance_chain(store, settings, chain_id=chain_id, client=client)
    assert outcome == "closed"

    chain = await store.get_chain(chain_id)
    assert chain["status"] == "closed"


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
