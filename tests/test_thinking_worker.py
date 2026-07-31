from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.thinking.settings import ALL_SEED_KINDS, ThinkingSettings
from app.thinking.store import ThoughtStore
from app.workers.thinking_worker import _CONSECUTIVE_FAILURES, tick


class FakeClient:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)

    async def complete(self, request: Any) -> str:
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


async def _quiet(_now: Any, _minutes: Any = None) -> bool:
    return True


async def _busy(_now: Any, _minutes: Any = None) -> bool:
    return False


async def _message(db, session_id: int = 1, *, minutes_ago: float, user_id: int = 7) -> None:
    """Insert a chat message whose ``created_at`` is ``minutes_ago`` in the past."""
    await db.execute(
        "INSERT OR IGNORE INTO chat_session(id, user_id, title) VALUES(?,?,?)",
        (session_id, user_id, "t"),
    )
    await db.execute(
        "INSERT INTO chat_message(session_id, role, content, created_at) "
        "VALUES(?, 'user', 'hi', datetime('now', ?))",
        (session_id, f"-{minutes_ago} minutes"),
    )
    await db.commit()


async def test_disabled_setting_stops_everything(db, monkeypatch) -> None:
    await _user(db)
    monkeypatch.setattr("app.workers.thinking_worker._is_quiet", _quiet)
    result = await tick(
        ThoughtStore(), _settings(enabled=False),
        persona_user_id=7, now=datetime.now(UTC), client=FakeClient([]),
    )
    assert result == "disabled"


async def test_owner_activity_blocks_thinking(db, monkeypatch) -> None:
    """Owner priority is an invariant: a busy model must never be used to think."""
    await _user(db)
    monkeypatch.setattr("app.workers.thinking_worker._is_quiet", _busy)
    result = await tick(
        ThoughtStore(), _settings(),
        persona_user_id=7, now=datetime.now(UTC), client=FakeClient(["мысль"]),
    )
    assert result == "busy"


async def test_daily_budget_stops_the_loop(db, monkeypatch) -> None:
    await _user(db)
    monkeypatch.setattr("app.workers.thinking_worker._is_quiet", _quiet)
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    await store.append_step(chain_id, text="one")
    result = await tick(
        store, _settings(daily_budget=2),
        persona_user_id=7, now=datetime.now(UTC), client=FakeClient(["ещё"]),
    )
    assert result == "budget"


async def test_quiet_and_no_open_chain_seeds_one(db, monkeypatch) -> None:
    """``alive`` is the only kind exempt from the evidence requirement, so it
    is the one used here to exercise the "seeded" outcome deterministically
    without needing owner chat history or memory facts on hand."""
    await _user(db)
    monkeypatch.setattr("app.workers.thinking_worker._is_quiet", _quiet)
    store = ThoughtStore()
    result = await tick(
        store, _settings(seed_kinds=("alive",)),
        persona_user_id=7, now=datetime.now(UTC), client=FakeClient(["новая мысль"]),
    )
    assert result == "seeded"
    assert (await store.oldest_open_chain(7)) is not None


async def test_no_evidence_seed_refusal_is_idle_not_an_error(db, monkeypatch) -> None:
    """An evidence-dependent seed kind with no owner evidence on hand must
    refuse to seed (no model call, no chain) — that is normal, not a bug."""
    await _user(db)
    monkeypatch.setattr("app.workers.thinking_worker._is_quiet", _quiet)
    store = ThoughtStore()
    result = await tick(
        store, _settings(seed_kinds=("know_you",)),
        persona_user_id=7, now=datetime.now(UTC), client=FakeClient(["новая мысль"]),
    )
    assert result == "idle"
    assert (await store.oldest_open_chain(7)) is None


async def test_quiet_with_an_open_chain_advances_it(db, monkeypatch) -> None:
    await _user(db)
    monkeypatch.setattr("app.workers.thinking_worker._is_quiet", _quiet)
    store = ThoughtStore()
    await store.open_chain(
        7, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    result = await tick(
        store, _settings(),
        persona_user_id=7, now=datetime.now(UTC), client=FakeClient(["шаг"]),
    )
    assert result == "stepped"


async def test_chain_force_closes_after_repeated_failures(db, monkeypatch) -> None:
    """A chain stuck failing at its cap must not retry forever and burn budget."""
    await _user(db)
    monkeypatch.setattr("app.workers.thinking_worker._is_quiet", _quiet)
    _CONSECUTIVE_FAILURES.clear()
    store = ThoughtStore()
    chain_id = await store.open_chain(
        7, seed_text="s", seed_kind="alive",
        source_scope="owner_private", source_session_id=None,
    )
    # step_cap=1 and one existing step means the chain is already at its cap,
    # so every advance_chain call will attempt (and, with empty replies, fail
    # to produce) a conclusion.
    await store.append_step(chain_id, text="one")
    settings = _settings(step_cap=1)

    for _ in range(2):
        result = await tick(
            store, settings, persona_user_id=7, now=datetime.now(UTC),
            client=FakeClient([""]),
        )
        assert result == "failed"

    result = await tick(
        store, settings, persona_user_id=7, now=datetime.now(UTC),
        client=FakeClient([""]),
    )
    assert result == "closed"

    reopened = await store.oldest_open_chain(7)
    assert reopened is None
    steps = await store.chain_steps(chain_id)
    assert steps[-1]["kind"] == "conclusion"
    assert "прерв" in steps[-1]["text"]
    assert chain_id not in _CONSECUTIVE_FAILURES


async def test_quiet_minutes_setting_thinks_when_quiet_long_enough(db) -> None:
    """quiet_minutes=3, last message 4 minutes ago: the owner is quiet enough."""
    await _user(db)
    await _message(db, minutes_ago=4)
    result = await tick(
        ThoughtStore(), _settings(quiet_minutes=3, seed_kinds=("alive",)),
        persona_user_id=7, now=datetime.now(UTC), client=FakeClient(["новая мысль"]),
    )
    assert result == "seeded"


async def test_quiet_minutes_setting_blocks_when_too_recent(db) -> None:
    """quiet_minutes=3, last message 2 minutes ago: still owner-active, must not think."""
    await _user(db)
    await _message(db, minutes_ago=2)
    result = await tick(
        ThoughtStore(), _settings(quiet_minutes=3, seed_kinds=("alive",)),
        persona_user_id=7, now=datetime.now(UTC), client=FakeClient(["новая мысль"]),
    )
    assert result == "busy"


async def test_research_chain_runs_under_the_short_gate_not_the_full_setting(db) -> None:
    """A research chain must not wait behind the (much longer) self-directed
    quiet_minutes setting: 40s of quiet is not enough for a 3-minute
    self-directed gate, but is enough for the ~30s research gate. The
    chain's first advance always does a real (deterministic) web_search
    before any model call, so the outcome here is "stepped" or "closed"
    depending on whether that search found anything — either is proof the
    gate let the tick through instead of returning "busy"."""
    await _user(db)
    await _message(db, minutes_ago=40 / 60)
    store = ThoughtStore()
    await store.open_chain(
        7, seed_text="что за Лабиринт Фавна", seed_kind="research",
        source_scope="group", source_session_id=None,
    )
    result = await tick(
        store, _settings(quiet_minutes=3),
        persona_user_id=7, now=datetime.now(UTC), client=FakeClient(["шаг"]),
    )
    assert result in ("stepped", "closed")


async def test_owner_activity_inside_research_window_still_blocks(db) -> None:
    """Even a waiting research request must not cut in front of the owner:
    10 seconds of quiet is under the ~30s research gate too."""
    await _user(db)
    await _message(db, minutes_ago=10 / 60)
    store = ThoughtStore()
    await store.open_chain(
        7, seed_text="что за Лабиринт Фавна", seed_kind="research",
        source_scope="group", source_session_id=None,
    )
    result = await tick(
        store, _settings(quiet_minutes=3),
        persona_user_id=7, now=datetime.now(UTC), client=FakeClient(["шаг"]),
    )
    assert result == "busy"
