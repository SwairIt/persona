"""Owner/privacy, policy and durable delivery contracts for autowake."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.autowake import SqliteAutowakeRepository
from app.application.autowake import (
    AutowakeDispatcher,
    AutowakeService,
    EnqueueAutowake,
    IdempotencyConflict,
    OwnerTelegramDelivery,
    enqueue_completed_briefing,
    enqueue_completed_dream_report,
)
from app.domains.autowake import (
    AutowakePolicy,
    AutowakePolicyConfig,
    DeliveryState,
    SourceScope,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def _command(
    *,
    owner_user_id: int = 7,
    is_owner: bool = True,
    key: str = "briefing:2026-07-28",
    text: str = "Твой короткий утренний брифинг готов.",
    source: str = "briefing",
    scope: SourceScope = SourceScope.DERIVED_OWNER,
) -> EnqueueAutowake:
    return EnqueueAutowake(
        owner_user_id=owner_user_id,
        is_owner=is_owner,
        kind="daily.briefing",
        source=source,
        source_scope=scope,
        text=text,
        idempotency_key=key,
    )


class _Gateway:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.deliveries: list[OwnerTelegramDelivery] = []

    async def send_owner(self, delivery: OwnerTelegramDelivery) -> None:
        if self.failures:
            self.failures -= 1
            raise ConnectionError("https://bot-token:secret@example.invalid")
        self.deliveries.append(delivery)


def test_policy_enforces_quiet_hours_cooldown_and_daily_cap() -> None:
    policy = AutowakePolicy()
    quiet_now = NOW.replace(hour=23, minute=30)
    quiet = policy.evaluate(now=quiet_now, state=DeliveryState())
    assert quiet.kind == "defer"
    assert quiet.reason == "quiet_hours"
    assert quiet.due_at == datetime(2026, 7, 29, 8, tzinfo=UTC)

    cooldown = policy.evaluate(
        now=NOW,
        state=DeliveryState(last_delivered_at=NOW - timedelta(minutes=10)),
    )
    assert cooldown.kind == "defer"
    assert cooldown.reason == "cooldown"
    assert cooldown.due_at == NOW + timedelta(hours=1, minutes=50)

    capped = policy.evaluate(
        now=NOW,
        state=DeliveryState(delivered_today=policy.config.daily_cap),
    )
    assert capped.kind == "defer"
    assert capped.reason == "daily_cap"
    assert capped.due_at == datetime(2026, 7, 29, 8, tzinfo=UTC)


@pytest.mark.asyncio
async def test_non_owner_and_wrong_owner_are_rejected_before_persistence(db) -> None:
    service = AutowakeService(
        SqliteAutowakeRepository(),
        expected_owner_user_id=7,
    )
    with pytest.raises(PermissionError):
        await service.enqueue(_command(is_owner=False), now=NOW)
    with pytest.raises(PermissionError):
        await service.enqueue(_command(owner_user_id=8), now=NOW)
    row = await (await db.execute("SELECT COUNT(*) AS n FROM autowake_event")).fetchone()
    assert int(row["n"]) == 0


@pytest.mark.asyncio
async def test_group_and_secret_content_never_enter_message_or_outbox(db) -> None:
    service = AutowakeService(
        SqliteAutowakeRepository(),
        expected_owner_user_id=7,
    )
    group = await service.enqueue(
        _command(
            key="group:123",
            text="Sensitive words copied from a group",
            scope=SourceScope.GROUP,
        ),
        now=NOW,
    )
    secret = await service.enqueue(
        _command(
            key="secret:123",
            text="BOT_TOKEN=123456789:abcdefghijklmnopqrstuvwxyzABCDEFGH",
            scope=SourceScope.OWNER_PRIVATE,
        ),
        now=NOW,
    )
    assert not group.accepted and group.reason == "unsafe_source_scope:group"
    assert not secret.accepted and secret.reason == "secret_like_content"

    events = await (
        await db.execute(
            "SELECT status, rejection_reason, idempotency_key, "
            "content_fingerprint FROM autowake_event ORDER BY id"
        )
    ).fetchall()
    assert [str(row["status"]) for row in events] == ["rejected", "rejected"]
    assert all(str(row["idempotency_key"]).startswith("rejected:") for row in events)
    assert all(row["content_fingerprint"] == "0" * 64 for row in events)
    for table in ("autowake_session", "autowake_message", "autowake_outbox"):
        row = await (await db.execute(f"SELECT COUNT(*) AS n FROM {table}")).fetchone()
        assert int(row["n"]) == 0


@pytest.mark.asyncio
async def test_enqueue_duplicate_is_suppressed_and_conflict_fails(db) -> None:
    service = AutowakeService(
        SqliteAutowakeRepository(),
        expected_owner_user_id=7,
    )
    first = await service.enqueue(_command(), now=NOW)
    duplicate = await service.enqueue(_command(), now=NOW)
    assert first.created and first.accepted
    assert duplicate.event_id == first.event_id
    assert duplicate.outbox_id == first.outbox_id
    assert not duplicate.created

    with pytest.raises(IdempotencyConflict):
        await service.enqueue(
            _command(text="Другой текст под тем же ключом"),
            now=NOW,
        )
    row = await (await db.execute("SELECT COUNT(*) AS n FROM autowake_outbox")).fetchone()
    assert int(row["n"]) == 1


@pytest.mark.asyncio
async def test_atomic_lease_claim_and_expiry_recovery(db) -> None:
    repository = SqliteAutowakeRepository()
    service = AutowakeService(repository, expected_owner_user_id=7)
    await service.enqueue(_command(), now=NOW)

    first, second = await asyncio.gather(
        repository.claim_due(lease_owner="worker-a", now=NOW, lease_seconds=15),
        repository.claim_due(lease_owner="worker-b", now=NOW, lease_seconds=15),
    )
    claimed = [item for item in (first, second) if item is not None]
    assert len(claimed) == 1
    original_worker = claimed[0].lease_owner
    recovery_worker = "worker-b" if original_worker == "worker-a" else "worker-a"

    recovered = await repository.claim_due(
        lease_owner=recovery_worker,
        now=NOW + timedelta(seconds=16),
        lease_seconds=15,
    )
    assert recovered is not None
    assert recovered.lease_owner == recovery_worker
    assert recovered.attempts == 1


@pytest.mark.asyncio
async def test_expired_started_attempt_is_not_counted_twice(db) -> None:
    repository = SqliteAutowakeRepository()
    service = AutowakeService(repository, expected_owner_user_id=7)
    await service.enqueue(_command(key="lease:started"), now=NOW)
    claimed = await repository.claim_due(
        lease_owner="worker-started",
        now=NOW,
        lease_seconds=15,
    )
    assert claimed is not None
    assert (
        await repository.start_attempt(
            claimed.id,
            lease_owner="worker-started",
            now=NOW,
        )
        == 1
    )

    recovered = await repository.claim_due(
        lease_owner="worker-recovery",
        now=NOW + timedelta(seconds=16),
        lease_seconds=15,
    )
    assert recovered is not None
    assert recovered.attempts == 1


@pytest.mark.asyncio
async def test_retry_then_owner_only_delivery_and_secret_safe_error(db) -> None:
    repository = SqliteAutowakeRepository()
    policy = AutowakePolicy()
    service = AutowakeService(
        repository,
        expected_owner_user_id=7,
        policy=policy,
    )
    await service.enqueue(_command(), now=NOW)
    gateway = _Gateway(failures=1)
    dispatcher = AutowakeDispatcher(
        repository,
        gateway,
        policy=policy,
        lease_owner="dispatcher-a",
        expected_owner_user_id=7,
    )

    assert await dispatcher.run_once(now=NOW)
    failed = await (
        await db.execute("SELECT status, attempts, last_error_code, due_at FROM autowake_outbox")
    ).fetchone()
    assert failed["status"] == "retry"
    assert int(failed["attempts"]) == 1
    assert failed["last_error_code"] == "ConnectionError"
    assert "secret" not in str(failed["last_error_code"])

    assert await dispatcher.run_once(now=NOW + timedelta(seconds=31))
    assert len(gateway.deliveries) == 1
    delivered = gateway.deliveries[0]
    assert delivered.owner_user_id == 7
    assert delivered.text == _command().text
    assert not hasattr(delivered, "chat_id")
    row = await (await db.execute("SELECT status, delivered_at FROM autowake_outbox")).fetchone()
    assert row["status"] == "delivered"
    assert row["delivered_at"] is not None


@pytest.mark.asyncio
async def test_dispatcher_refuses_corrupted_non_owner_target(db) -> None:
    repository = SqliteAutowakeRepository()
    service = AutowakeService(repository, expected_owner_user_id=7)
    await service.enqueue(_command(), now=NOW)
    await db.execute("UPDATE autowake_outbox SET owner_user_id=8")
    await db.commit()
    gateway = _Gateway()
    dispatcher = AutowakeDispatcher(
        repository,
        gateway,
        lease_owner="owner-guard",
        expected_owner_user_id=7,
    )
    with pytest.raises(PermissionError, match="configured owner"):
        await dispatcher.run_once(now=NOW)
    assert gateway.deliveries == []


@pytest.mark.asyncio
async def test_failures_reach_dead_letter_at_bounded_attempt_count(db) -> None:
    config = AutowakePolicyConfig(
        max_attempts=2,
        retry_base=timedelta(seconds=1),
        retry_max=timedelta(seconds=2),
    )
    policy = AutowakePolicy(config)
    repository = SqliteAutowakeRepository()
    service = AutowakeService(
        repository,
        expected_owner_user_id=7,
        policy=policy,
    )
    await service.enqueue(_command(), now=NOW)
    dispatcher = AutowakeDispatcher(
        repository,
        _Gateway(failures=2),
        policy=policy,
        lease_owner="dispatcher-dead",
        expected_owner_user_id=7,
    )
    assert await dispatcher.run_once(now=NOW)
    assert await dispatcher.run_once(now=NOW + timedelta(seconds=2))

    outbox = await (await db.execute("SELECT status, attempts FROM autowake_outbox")).fetchone()
    event = await (await db.execute("SELECT status FROM autowake_event")).fetchone()
    session = await (await db.execute("SELECT status FROM autowake_session")).fetchone()
    assert (outbox["status"], int(outbox["attempts"])) == ("dead", 2)
    assert event["status"] == "dead"
    assert session["status"] == "dead"


@pytest.mark.asyncio
async def test_existing_quiet_rule_defers_until_rule_end(db) -> None:
    await db.execute(
        """
        INSERT INTO quiet_hours(weekday, start_hour, end_hour, label)
        VALUES(?, 12, 13, 'lunch')
        """,
        (NOW.weekday(),),
    )
    await db.commit()
    service = AutowakeService(
        SqliteAutowakeRepository(),
        expected_owner_user_id=7,
    )
    result = await service.enqueue(_command(), now=NOW)
    assert result.accepted
    assert result.reason == "quiet_hours"
    assert result.due_at == NOW.replace(hour=13)

    repository = SqliteAutowakeRepository()
    assert (
        await repository.claim_due(
            lease_owner="quiet-worker",
            now=NOW,
            lease_seconds=30,
        )
        is None
    )


@pytest.mark.asyncio
async def test_completed_producers_are_deterministic_and_owner_private(db) -> None:
    service = AutowakeService(
        SqliteAutowakeRepository(),
        expected_owner_user_id=7,
    )
    first = await enqueue_completed_briefing(
        service,
        owner_user_id=7,
        slot="morning",
        title="Утренняя сводка",
        body="Сегодня один главный приоритет.",
        completed_at=NOW,
    )
    duplicate = await enqueue_completed_briefing(
        service,
        owner_user_id=7,
        slot="morning",
        title="Утренняя сводка",
        body="Сегодня один главный приоритет.",
        completed_at=NOW + timedelta(minutes=10),
    )
    assert first.created
    assert not duplicate.created
    event = await (
        await db.execute(
            "SELECT kind, source, source_scope, idempotency_key "
            "FROM autowake_event WHERE id=?",
            (first.event_id,),
        )
    ).fetchone()
    assert tuple(event) == (
        "briefing.completed",
        "briefing",
        "derived_owner",
        "briefing:morning:2026-07-28",
    )

    with pytest.raises(PermissionError, match="group-derived"):
        await enqueue_completed_dream_report(
            service,
            owner_user_id=7,
            dream_run_id=11,
            report="Group-derived report must not be delivered.",
            completed_at=NOW,
            owner_private_only=False,
        )
