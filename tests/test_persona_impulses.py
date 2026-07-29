"""Proactive Persona impulse safety, policy and durability contracts."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.autowake import SqliteAutowakeRepository
from app.application.autowake import (
    AutowakeDispatcher,
    AutowakeService,
    ImpulseContext,
    PersonaImpulseProducer,
)
from app.domains.autowake import (
    AutowakePolicy,
    DeliveryState,
    DeliveryTarget,
    DeliveryTargetKind,
    SourceScope,
)
from app.storage.db import write_transaction

NOW = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)


class _Context:
    def __init__(self, value: ImpulseContext | None) -> None:
        self.value = value
        self.calls = 0

    async def next_context(self, *, owner_user_id: int, now: datetime):
        self.calls += 1
        return self.value


class _Decision:
    def __init__(self, *values: object) -> None:
        self.values = list(values)
        self.calls = 0

    async def decide(self, context: ImpulseContext) -> str | None:
        value = self.values[self.calls]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        return str(value) if value is not None else None


class _PolicyRepository:
    def __init__(self, state: DeliveryState) -> None:
        self.state = state

    async def policy_state(self, owner_user_id: int, *, now: datetime) -> DeliveryState:
        return self.state


class _UnusedAutowake:
    async def enqueue(self, command, *, now: datetime):
        raise AssertionError("enqueue must not run while the policy gate is closed")


class _Gateway:
    def __init__(self) -> None:
        self.owner = []
        self.group = []

    async def send_owner(self, delivery) -> None:
        self.owner.append(delivery)

    async def send_group(self, delivery) -> None:
        self.group.append(delivery)


def _owner_context() -> ImpulseContext:
    return ImpulseContext(
        owner_user_id=7,
        target=DeliveryTarget(),
        source_scope=SourceScope.OWNER_PRIVATE,
        provenance="telegram_owner_dm",
        excerpts=("user: Сегодня важная встреча",),
    )


def _producer(repository, context, decision) -> PersonaImpulseProducer:
    policy = AutowakePolicy()
    return PersonaImpulseProducer(
        repository,
        AutowakeService(repository, expected_owner_user_id=7, policy=policy),
        context,
        decision,
        owner_user_id=7,
        policy=policy,
    )


@pytest.mark.asyncio
async def test_model_silent_creates_no_durable_rows(db) -> None:
    repository = SqliteAutowakeRepository()
    outcome = await _producer(
        repository,
        _Context(_owner_context()),
        _Decision("SILENT"),
    ).run_once(now=NOW)
    assert not outcome.emitted
    assert outcome.reason == "model_silent"
    row = await (await db.execute("SELECT COUNT(*) AS n FROM autowake_event")).fetchone()
    assert int(row["n"]) == 0


@pytest.mark.asyncio
async def test_enqueue_is_durable_and_same_slot_is_deduplicated(db) -> None:
    repository = SqliteAutowakeRepository()
    producer = _producer(
        repository,
        _Context(_owner_context()),
        _Decision("Не забудь подготовить вопросы.", "Другая формулировка."),
    )
    first = await producer.run_once(now=NOW)
    duplicate = await producer.run_once(now=NOW + timedelta(minutes=5))
    assert first.emitted
    assert not duplicate.emitted
    assert duplicate.reason == "duplicate_slot"
    row = await (
        await db.execute(
            "SELECT COUNT(*) AS n, MIN(channel) AS channel FROM autowake_outbox"
        )
    ).fetchone()
    assert (int(row["n"]), row["channel"]) == (1, "telegram_owner_dm")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "reason"),
    [
        (DeliveryState(last_delivered_at=NOW - timedelta(minutes=5)), "cooldown"),
        (DeliveryState(delivered_today=4), "daily_cap"),
        (DeliveryState(quiet_until=NOW + timedelta(hours=1)), "quiet_hours"),
    ],
)
async def test_policy_gate_skips_context_and_llm(state, reason) -> None:
    repository = _PolicyRepository(state)
    context = _Context(_owner_context())
    decision = _Decision("Не должно вызываться")
    producer = PersonaImpulseProducer(
        repository,
        _UnusedAutowake(),
        context,
        decision,
        owner_user_id=7,
    )
    outcome = await producer.run_once(now=NOW)
    assert not outcome.emitted and outcome.reason == reason
    assert context.calls == 0
    assert decision.calls == 0


def test_group_context_fails_closed_without_isolated_opt_in_provenance() -> None:
    with pytest.raises(ValueError, match="opt-in provenance"):
        ImpulseContext(
            owner_user_id=7,
            target=DeliveryTarget(DeliveryTargetKind.GROUP, -1001),
            source_scope=SourceScope.GROUP,
            provenance="telegram_group",
            excerpts=("user: group-only text",),
            group_opt_in_verified=False,
        )


@pytest.mark.asyncio
async def test_opted_in_group_is_enqueued_and_dispatched_to_explicit_target(db) -> None:
    context = ImpulseContext(
        owner_user_id=7,
        target=DeliveryTarget(DeliveryTargetKind.GROUP, -1001),
        source_scope=SourceScope.GROUP,
        provenance="telegram_group",
        excerpts=("user: Кто возьмёт на себя итог встречи?",),
        group_opt_in_verified=True,
    )
    repository = SqliteAutowakeRepository()
    producer = _producer(
        repository,
        _Context(context),
        _Decision("Могу кратко собрать итог встречи, если полезно."),
    )
    assert (await producer.run_once(now=NOW)).emitted
    row = await (
        await db.execute(
            "SELECT channel, target_chat_id FROM autowake_outbox"
        )
    ).fetchone()
    assert (row["channel"], int(row["target_chat_id"])) == ("telegram_group", -1001)

    gateway = _Gateway()
    dispatcher = AutowakeDispatcher(
        repository,
        gateway,
        lease_owner="impulse-test",
        expected_owner_user_id=7,
    )
    assert await dispatcher.run_once(now=NOW)
    assert gateway.owner == []
    assert len(gateway.group) == 1
    assert gateway.group[0].telegram_chat_id == -1001


@pytest.mark.asyncio
async def test_offline_llm_leaves_no_marker_and_next_attempt_can_enqueue(db) -> None:
    repository = SqliteAutowakeRepository()
    producer = _producer(
        repository,
        _Context(_owner_context()),
        _Decision(ConnectionError("offline"), "Я на связи, если хочешь обсудить встречу."),
    )
    with pytest.raises(ConnectionError):
        await producer.run_once(now=NOW)
    row = await (await db.execute("SELECT COUNT(*) AS n FROM autowake_event")).fetchone()
    assert int(row["n"]) == 0
    assert (await producer.run_once(now=NOW + timedelta(minutes=1))).emitted


@pytest.mark.asyncio
async def test_llm_decision_runs_without_an_open_repository_transaction(db) -> None:
    class _WritingDecision:
        async def decide(self, context: ImpulseContext) -> str:
            async with write_transaction() as conn:
                await conn.execute(
                    """
                    INSERT INTO kv_settings(key, value, updated_at)
                    VALUES('impulse_tx_probe', 'ok', datetime('now'))
                    ON CONFLICT(key) DO UPDATE SET value='ok'
                    """
                )
            return "Короткое сообщение."

    repository = SqliteAutowakeRepository()
    outcome = await asyncio.wait_for(
        _producer(
            repository,
            _Context(_owner_context()),
            _WritingDecision(),
        ).run_once(now=NOW),
        timeout=2,
    )
    assert outcome.emitted
