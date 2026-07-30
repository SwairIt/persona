from __future__ import annotations

from typing import Any

from app.thinking.loop import advance_chain, next_seed_kind, seed_chain
from app.thinking.settings import ALL_SEED_KINDS, ThinkingSettings
from app.thinking.store import ThoughtStore


class FakeClient:
    """Returns queued replies and records the requests it was given."""

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


async def _owner_message(db, user_id: int, text: str) -> None:
    """Insert a real owner chat message — the evidence seed_chain may quote."""
    cur = await db.execute(
        "INSERT INTO chat_session(user_id, title) VALUES(?, 't')", (user_id,)
    )
    session_id = cur.lastrowid
    await db.execute(
        "INSERT INTO chat_message(session_id, role, content) VALUES(?, 'user', ?)",
        (session_id, text),
    )
    await db.commit()


def _settings(**over: Any) -> ThinkingSettings:
    base = dict(
        enabled=True, cap_mode="fixed", step_cap=2, emergency_cap=50,
        daily_budget=60, seed_kinds=ALL_SEED_KINDS, may_write_to_chat=False,
    )
    base.update(over)
    return ThinkingSettings(**base)


async def test_seed_opens_a_chain_with_the_model_text(db) -> None:
    """``alive`` needs no evidence — a free thought is not a claim about anyone."""
    await _user(db)
    store = ThoughtStore()
    client = FakeClient(["ДОГАДКА: Почему он просит писать короче?"])
    chain_id = await seed_chain(
        store, persona_user_id=7, seed_kind="alive",
        source_scope="owner_private", source_session_id=11, client=client,
    )
    assert chain_id is not None
    steps = await store.chain_steps(chain_id)
    assert steps[0]["kind"] == "seed"
    assert steps[0]["text"] == "Почему он просит писать короче?"
    assert steps[0]["certainty"] == "guess"


async def test_empty_model_reply_seeds_nothing(db) -> None:
    await _user(db)
    store = ThoughtStore()
    assert await seed_chain(
        store, persona_user_id=7, seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
        client=FakeClient(["   "]),
    ) is None


async def test_evidence_dependent_kind_with_no_evidence_seeds_nothing(db) -> None:
    """No evidence, no thought: with no owner messages and no memory facts,
    ``know_you`` must return None and the model must never even be called."""
    await _user(db)
    store = ThoughtStore()
    client = FakeClient(["в любом случае что-то придуманное"])
    chain_id = await seed_chain(
        store, persona_user_id=7, seed_kind="know_you",
        source_scope="owner_private", source_session_id=None, client=client,
    )
    assert chain_id is None
    assert client.requests == []
    assert await store.oldest_open_chain(7) is None


async def test_evidence_dependent_kind_with_evidence_reaches_the_model(db) -> None:
    """With real evidence present, that evidence text must actually reach the
    model's request — not just exist somewhere unused."""
    await _user(db)
    marker = "ОСОБАЯ-ФРАЗА-ВЛАДЕЛЬЦА-ДЛЯ-ТЕСТА"
    await _owner_message(db, 7, f"Сегодня я думаю про {marker}.")
    store = ThoughtStore()
    client = FakeClient(["НАБЛЮДЕНИЕ: он думает про что-то конкретное"])
    chain_id = await seed_chain(
        store, persona_user_id=7, seed_kind="know_you",
        source_scope="owner_private", source_session_id=None, client=client,
    )
    assert chain_id is not None
    assert len(client.requests) == 1
    assert marker in client.requests[0].user
    steps = await store.chain_steps(chain_id)
    assert steps[0]["certainty"] == "observation"
    assert steps[0]["text"] == "он думает про что-то конкретное"


async def test_unmarked_reply_is_stored_as_a_guess(db) -> None:
    await _user(db)
    marker = "ДРУГАЯ-ФРАЗА-ВЛАДЕЛЬЦА"
    await _owner_message(db, 7, f"Заметка про {marker}.")
    store = ThoughtStore()
    client = FakeClient(["ответ без всякой пометки в начале"])
    chain_id = await seed_chain(
        store, persona_user_id=7, seed_kind="know_you",
        source_scope="owner_private", source_session_id=None, client=client,
    )
    assert chain_id is not None
    steps = await store.chain_steps(chain_id)
    assert steps[0]["certainty"] == "guess"
    assert steps[0]["text"] == "ответ без всякой пометки в начале"


async def test_previous_steps_are_fed_back_as_input(db) -> None:
    """The whole point: the chain's own text must reach the model."""
    await _user(db)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="СИД-МАРКЕР", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    client = FakeClient(["следующая мысль"])
    assert await advance_chain(store, _settings(), chain_id=chain_id, client=client) == "stepped"
    sent = client.requests[0]
    assert "СИД-МАРКЕР" in sent.user


async def test_chain_is_forced_closed_at_the_cap(db) -> None:
    await _user(db)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    settings = _settings(step_cap=2)
    client = FakeClient(["шаг один", "шаг два", "итог"])
    assert await advance_chain(store, settings, chain_id=chain_id, client=client) == "stepped"
    assert await advance_chain(store, settings, chain_id=chain_id, client=client) == "stepped"
    assert await advance_chain(store, settings, chain_id=chain_id, client=client) == "closed"
    steps = await store.chain_steps(chain_id)
    assert steps[-1]["kind"] == "conclusion"
    assert await store.oldest_open_chain(7) is None


async def test_model_decides_mode_closes_when_the_model_says_so(db) -> None:
    await _user(db)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    settings = _settings(cap_mode="model", step_cap=2, emergency_cap=50)
    client = FakeClient(["ХВАТИТ: всё понятно, вывод такой"])
    assert await advance_chain(store, settings, chain_id=chain_id, client=client) == "closed"


async def test_model_decides_mode_still_stops_at_the_emergency_cap(db) -> None:
    """The owner asked for model-decided depth; the emergency cap only stops a
    chain that never terminates."""
    await _user(db)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    settings = _settings(cap_mode="model", step_cap=2, emergency_cap=3)
    client = FakeClient(["ещё", "ещё", "ещё", "ещё", "ещё"])
    outcomes = [
        await advance_chain(store, settings, chain_id=chain_id, client=client)
        for _ in range(4)
    ]
    assert outcomes[-1] == "closed"


async def test_caller_supplied_client_is_never_pinned_to_a_model(db) -> None:
    """settings.model must only steer clients this module creates itself —
    a client the caller handed in may be reused elsewhere and must be left
    untouched, even when a model is configured."""
    await _user(db)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    client = FakeClient(["шаг один"])
    assert not hasattr(client, "_model")
    settings = _settings(model="qwen2.5:7b")
    await advance_chain(store, settings, chain_id=chain_id, client=client)
    assert not hasattr(client, "_model"), "caller-owned client must never be mutated"


def test_seed_kind_rotates_only_through_enabled_kinds() -> None:
    settings = ThinkingSettings(
        enabled=True, cap_mode="fixed", step_cap=5, emergency_cap=50,
        daily_budget=60, seed_kinds=("know_you", "alive"), may_write_to_chat=False,
    )
    assert next_seed_kind(settings, None) == "know_you"
    assert next_seed_kind(settings, "know_you") == "alive"
    assert next_seed_kind(settings, "alive") == "know_you"
    assert next_seed_kind(settings, "self_check") == "know_you"
