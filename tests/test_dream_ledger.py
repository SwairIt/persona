"""Safety and durability tests for the proposal-only dream pipeline."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import aiosqlite
import pytest

from app.adapters.memory.dream_repository import (
    DreamLeaseError,
    SqliteDreamLedger,
)
from app.application.memory.dream_service import DreamLedgerService
from app.application.memory.ports import DreamApplySummary, DreamCompletionReport
from app.auth.roles import delete_user
from app.chat import reflection
from app.domains.memory.dream import (
    DreamCandidate,
    DreamEvidence,
    DreamPolicy,
    MemorySnapshot,
    PolicyDecision,
)

if TYPE_CHECKING:
    from app.application.memory.ports import DreamRunLease


async def _user(db: aiosqlite.Connection, user_id: int = 1) -> None:
    await db.execute(
        "INSERT INTO users(id, email, password_hash) VALUES(?,?,?)",
        (user_id, f"{user_id}@example.test", "x"),
    )
    await db.commit()


def _evidence(
    *,
    source_kind: str = "owner_chat",
    source_ref: str = "chat:42",
    message_id: int | None = 10,
    owner_attributed: bool = True,
) -> DreamEvidence:
    return DreamEvidence(
        source_kind=source_kind,
        source_ref=source_ref,
        source_message_id=message_id,
        owner_attributed=owner_attributed,
        content_hash=hashlib.sha256(source_ref.encode()).hexdigest(),
        excerpt="durable evidence",
        observed_at="2026-07-28 02:00:00",
    )


def _candidate(
    text: str = "Пользователь развивает проект Persona",
    *,
    evidence: tuple[DreamEvidence, ...] | None = None,
    action: str = "add",
    target: int | None = None,
) -> DreamCandidate:
    return DreamCandidate(
        key=hashlib.sha256(f"{action}|{text}".encode()).hexdigest(),
        text=text,
        kind="project",
        proposed_action=action,  # type: ignore[arg-type]
        target_memory_id=target,
        score=0.91,
        observed_count=2,
        source_count=1,
        evidence=evidence if evidence is not None else (_evidence(),),
    )


async def _lease(
    ledger: SqliteDreamLedger,
    *,
    key: str = "run-1",
    worker: str = "worker-a",
) -> DreamRunLease:
    return await ledger.acquire_run(
        user_id=1,
        idempotency_key=key,
        worker_id=worker,
        input_cursor=7,
        config={"threshold": 0.6, "min_recall_count": 1},
        lease_seconds=120,
    )


def test_policy_requires_attributed_owner_evidence() -> None:
    group = _candidate(
        evidence=(
            _evidence(
                source_kind="telegram_group",
                source_ref="chat:99",
                owner_attributed=False,
            ),
        )
    )
    decision = DreamPolicy(score_threshold=0.6, min_recall_count=1).decide(group, ())
    assert decision.action == "reject"
    assert decision.reason == "missing_trusted_owner_evidence"


def test_policy_never_updates_pinned_memory() -> None:
    pinned = MemorySnapshot(
        id=9,
        text="Пользователь живёт в Москве",
        kind="fact",
        pinned=True,
    )
    proposal = _candidate(
        "Пользователь переехал в Берлин",
        action="update",
        target=9,
    )
    decision = DreamPolicy(score_threshold=0.6, min_recall_count=1).decide(
        proposal, (pinned,)
    )
    assert decision.action == "reject"
    assert decision.reason == "pinned_memory_is_immutable"


@pytest.mark.asyncio
async def test_run_acquire_is_idempotent_and_expired_lease_is_reclaimed(
    db: aiosqlite.Connection,
) -> None:
    await _user(db)
    ledger = SqliteDreamLedger()
    first = await _lease(ledger)
    same = await _lease(ledger)
    assert first.acquired and same.acquired
    assert first.run_id == same.run_id
    assert same.attempt_count == 1

    await db.execute(
        "UPDATE dream_run SET lease_until=datetime('now', '-1 second') WHERE id=?",
        (first.run_id,),
    )
    await db.commit()
    reclaimed = await _lease(ledger, worker="worker-b")
    assert reclaimed.acquired
    assert reclaimed.run_id == first.run_id
    assert reclaimed.attempt_count == 2


@pytest.mark.asyncio
async def test_proposal_is_durable_before_policy_applies_memory(
    db: aiosqlite.Connection,
) -> None:
    await _user(db)
    ledger = SqliteDreamLedger()
    lease = await _lease(ledger)
    stored = await ledger.store_proposals(lease, (_candidate(),))

    candidate_row = await (
        await db.execute("SELECT status FROM dream_candidate WHERE run_id=?", (lease.run_id,))
    ).fetchone()
    evidence_count = await (
        await db.execute("SELECT COUNT(*) FROM dream_evidence")
    ).fetchone()
    memory_count = await (
        await db.execute("SELECT COUNT(*) FROM user_memory")
    ).fetchone()
    assert len(stored) == 1
    assert candidate_row["status"] == "proposed"
    assert evidence_count[0] == 1
    assert memory_count[0] == 0


@pytest.mark.asyncio
async def test_policy_apply_adds_memory_and_append_only_revision(
    db: aiosqlite.Connection,
) -> None:
    await _user(db)
    ledger = SqliteDreamLedger()
    lease = await _lease(ledger)
    summary = await DreamLedgerService(ledger).apply_proposals(
        lease,
        (_candidate(),),
        DreamPolicy(score_threshold=0.6, min_recall_count=1),
    )

    memory = await (await db.execute("SELECT * FROM user_memory")).fetchone()
    candidate = await (await db.execute("SELECT * FROM dream_candidate")).fetchone()
    revision = await (await db.execute("SELECT * FROM dream_revision")).fetchone()
    assert summary == DreamApplySummary(candidates=1, applied=1, rejected=0, noops=0)
    assert memory["pinned"] == 0
    assert memory["importance_source"] == "dream_policy"
    assert candidate["status"] == "applied"
    assert revision["action"] == "add"

    with pytest.raises(aiosqlite.IntegrityError, match="append-only"):
        await db.execute("UPDATE dream_revision SET action='noop'")


@pytest.mark.asyncio
async def test_reapplying_same_proposal_is_idempotent(
    db: aiosqlite.Connection,
) -> None:
    await _user(db)
    ledger = SqliteDreamLedger()
    lease = await _lease(ledger)
    proposal = _candidate()
    service = DreamLedgerService(ledger)
    policy = DreamPolicy(score_threshold=0.6, min_recall_count=1)

    first = await service.apply_proposals(lease, (proposal,), policy)
    second = await service.apply_proposals(lease, (proposal,), policy)

    memory_count = await (
        await db.execute("SELECT COUNT(*) FROM user_memory")
    ).fetchone()
    candidate_count = await (
        await db.execute("SELECT COUNT(*) FROM dream_candidate")
    ).fetchone()
    evidence_count = await (
        await db.execute("SELECT COUNT(*) FROM dream_evidence")
    ).fetchone()
    revision_count = await (
        await db.execute("SELECT COUNT(*) FROM dream_revision")
    ).fetchone()
    assert first.applied == second.applied == 1
    assert memory_count[0] == candidate_count[0] == evidence_count[0] == revision_count[0] == 1


@pytest.mark.asyncio
async def test_adapter_rechecks_pinned_target_against_forged_approval(
    db: aiosqlite.Connection,
) -> None:
    await _user(db)
    cursor = await db.execute(
        """
        INSERT INTO user_memory(user_id, kind, text, pinned)
        VALUES(1, 'fact', 'Пользователь живёт в Москве', 1)
        """
    )
    await db.commit()
    pinned_id = int(cursor.lastrowid)
    ledger = SqliteDreamLedger()
    lease = await _lease(ledger)
    stored = await ledger.store_proposals(
        lease,
        (
            _candidate(
                "Пользователь переехал в Берлин",
                action="update",
                target=pinned_id,
            ),
        ),
    )

    result = await ledger.apply_decision(
        lease,
        stored[0],
        PolicyDecision("update", "forged approval", pinned_id),
    )
    memories = await ledger.list_memories(1)
    assert result == "rejected"
    assert len(memories) == 1
    assert memories[0].id == pinned_id
    assert memories[0].pinned
    assert memories[0].active


@pytest.mark.asyncio
async def test_apply_rolls_back_memory_and_candidate_when_audit_write_fails(
    db: aiosqlite.Connection,
) -> None:
    await _user(db)
    ledger = SqliteDreamLedger()
    lease = await _lease(ledger)
    stored = await ledger.store_proposals(lease, (_candidate(),))
    await db.execute(
        """
        CREATE TRIGGER fail_dream_revision
        BEFORE INSERT ON dream_revision
        BEGIN
            SELECT RAISE(ABORT, 'simulated revision failure');
        END
        """
    )
    await db.commit()

    with pytest.raises(aiosqlite.IntegrityError, match="simulated revision failure"):
        await ledger.apply_decision(
            lease,
            stored[0],
            PolicyDecision("add", "trusted_supported_add"),
        )

    count = await (await db.execute("SELECT COUNT(*) FROM user_memory")).fetchone()
    candidate = await (await db.execute("SELECT status FROM dream_candidate")).fetchone()
    assert count[0] == 0
    assert candidate["status"] == "proposed"


@pytest.mark.asyncio
async def test_retry_does_not_advance_cursor_but_completion_is_atomic(
    db: aiosqlite.Connection,
) -> None:
    await _user(db)
    ledger = SqliteDreamLedger()
    lease = await _lease(ledger)
    await ledger.retry_run(
        lease,
        error="provider unavailable",
        retry_seconds=1,
        safe_cursor=7,
    )
    marker = await (
        await db.execute(
            "SELECT value FROM kv_settings WHERE key='dream_last_processed_message_id'"
        )
    ).fetchone()
    assert marker is None

    await db.execute(
        "UPDATE dream_run SET retry_at=datetime('now', '-1 second') WHERE id=?",
        (lease.run_id,),
    )
    await db.commit()
    retry_lease = await _lease(ledger, worker="worker-b")
    await ledger.complete_run(
        retry_lease,
        safe_cursor=12,
        summary=DreamApplySummary(candidates=0, applied=0, rejected=0, noops=0),
        report=DreamCompletionReport(),
    )
    run = await (
        await db.execute("SELECT status, safe_cursor FROM dream_run WHERE id=?", (lease.run_id,))
    ).fetchone()
    marker = await (
        await db.execute(
            "SELECT value FROM kv_settings WHERE key='dream_last_processed_message_id'"
        )
    ).fetchone()
    assert tuple(run) == ("completed", 12)
    assert marker["value"] == "12"


@pytest.mark.asyncio
async def test_report_failure_rolls_back_completion_cursor_and_reflection(
    db: aiosqlite.Connection,
) -> None:
    await _user(db)
    ledger = SqliteDreamLedger()
    lease = await _lease(ledger)
    await db.execute(
        """
        CREATE TRIGGER fail_dream_report
        BEFORE INSERT ON dream_report
        BEGIN
            SELECT RAISE(ABORT, 'simulated report failure');
        END
        """
    )
    await db.commit()

    with pytest.raises(aiosqlite.IntegrityError, match="simulated report failure"):
        await ledger.complete_run(
            lease,
            safe_cursor=12,
            summary=DreamApplySummary(candidates=1, applied=1, rejected=0, noops=0),
            report=DreamCompletionReport(
                dream_text="Пользователь развивает Persona",
                source_message_ids=(10,),
                impact_score=1.0,
            ),
        )

    run = await (
        await db.execute(
            "SELECT status, safe_cursor, worker_id FROM dream_run WHERE id=?",
            (lease.run_id,),
        )
    ).fetchone()
    marker = await (
        await db.execute(
            "SELECT value FROM kv_settings WHERE key='dream_last_processed_message_id'"
        )
    ).fetchone()
    report_count = await (
        await db.execute("SELECT COUNT(*) FROM dream_report")
    ).fetchone()
    reflection_count = await (
        await db.execute("SELECT COUNT(*) FROM reflection")
    ).fetchone()
    completed_audit_count = await (
        await db.execute(
            "SELECT COUNT(*) FROM dream_audit WHERE event='run_completed'"
        )
    ).fetchone()
    assert tuple(run) == ("running", 7, lease.worker_id)
    assert marker is None
    assert report_count[0] == reflection_count[0] == completed_audit_count[0] == 0

    await ledger.retry_run(
        lease,
        error="report persistence failed",
        retry_seconds=1,
        safe_cursor=7,
    )
    status = await (
        await db.execute("SELECT status FROM dream_run WHERE id=?", (lease.run_id,))
    ).fetchone()
    assert status["status"] == "retry"


@pytest.mark.asyncio
async def test_standard_user_delete_privacy_purges_full_dream_ledger(
    db: aiosqlite.Connection,
) -> None:
    await _user(db)
    ledger = SqliteDreamLedger()
    lease = await _lease(ledger)
    summary = await DreamLedgerService(ledger).apply_proposals(
        lease,
        (_candidate(),),
        DreamPolicy(score_threshold=0.6, min_recall_count=1),
    )
    await ledger.complete_run(
        lease,
        safe_cursor=12,
        summary=summary,
        report=DreamCompletionReport(
            dream_text="Пользователь регулярно развивает Persona",
            source_message_ids=(10,),
            impact_score=1.0,
        ),
    )

    for table in (
        "dream_run",
        "dream_candidate",
        "dream_evidence",
        "dream_revision",
        "dream_audit",
        "dream_report",
        "reflection",
        "user_memory",
    ):
        count = await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
        assert count[0] > 0, table

    assert await delete_user(1) is True

    for table in (
        "users",
        "dream_run",
        "dream_candidate",
        "dream_evidence",
        "dream_revision",
        "dream_audit",
        "dream_report",
        "dream_privacy_purge_guard",
        "reflection",
        "user_memory",
    ):
        count = await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
        assert count[0] == 0, table


@pytest.mark.asyncio
async def test_operational_ledger_deletes_remain_blocked(
    db: aiosqlite.Connection,
) -> None:
    await _user(db)
    ledger = SqliteDreamLedger()
    lease = await _lease(ledger)
    await DreamLedgerService(ledger).apply_proposals(
        lease,
        (_candidate(),),
        DreamPolicy(score_threshold=0.6, min_recall_count=1),
    )

    for table in (
        "dream_evidence",
        "dream_revision",
        "dream_audit",
        "dream_candidate",
        "dream_run",
    ):
        with pytest.raises(aiosqlite.IntegrityError, match="append-only"):
            await db.execute(f"DELETE FROM {table}")
        await db.rollback()


@pytest.mark.asyncio
async def test_lost_lease_cannot_apply_or_complete(
    db: aiosqlite.Connection,
) -> None:
    await _user(db)
    ledger = SqliteDreamLedger()
    lease = await _lease(ledger)
    await db.execute(
        "UPDATE dream_run SET lease_until=datetime('now', '-1 second') WHERE id=?",
        (lease.run_id,),
    )
    await db.commit()

    with pytest.raises(DreamLeaseError):
        await ledger.store_proposals(lease, (_candidate(),))
    with pytest.raises(DreamLeaseError):
        await ledger.complete_run(
            lease,
            safe_cursor=100,
            summary=DreamApplySummary(candidates=0, applied=0, rejected=0, noops=0),
            report=DreamCompletionReport(),
        )


@pytest.mark.asyncio
async def test_group_only_candidate_is_audited_but_not_promoted(
    db: aiosqlite.Connection,
) -> None:
    await _user(db)
    ledger = SqliteDreamLedger()
    lease = await _lease(ledger)
    proposal = _candidate(
        evidence=(
            _evidence(
                source_kind="telegram_group",
                source_ref="chat:77",
                owner_attributed=False,
            ),
        )
    )
    summary = await DreamLedgerService(ledger).apply_proposals(
        lease,
        (proposal,),
        DreamPolicy(score_threshold=0.6, min_recall_count=1),
    )

    memory_count = await (
        await db.execute("SELECT COUNT(*) FROM user_memory")
    ).fetchone()
    candidate = await (
        await db.execute("SELECT status, policy_reason FROM dream_candidate")
    ).fetchone()
    assert summary.rejected == 1
    assert memory_count[0] == 0
    assert tuple(candidate) == ("rejected", "missing_trusted_owner_evidence")


@pytest.mark.asyncio
async def test_full_cycle_promotes_only_after_ledger_and_advances_cursor(
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _user(db)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    docs = [
        {
            "source": f"chat:{index}",
            "text": "Я развиваю проект Persona каждый день",
            "message_ids": [10 + index],
            "latest_at": stamp,
        }
        for index in range(3)
    ]

    class FakeClient:
        async def complete_json(self, *_args, **_kwargs):
            return {
                "facts": [
                    {
                        "text": "Пользователь развивает проект Persona",
                        "kind": "project",
                    }
                ]
            }

        async def complete(self, *_args, **_kwargs):
            return "Пользователь регулярно развивает свой основной проект Persona."

    async def quiet(_now):
        return True

    async def gather(_user_id, _cutoff, _last):
        return docs, {10, 11, 12}, set()

    monkeypatch.setattr(reflection, "_is_quiet", quiet)
    monkeypatch.setattr(reflection, "_gather_documents", gather)
    monkeypatch.setattr("app.llm.client.make_client", lambda **_kwargs: FakeClient())

    result = await reflection.run_dream_cycle(1)

    assert result["status"] == "ok"
    assert result["promoted"] == 1
    run = await (
        await db.execute("SELECT status, safe_cursor FROM dream_run")
    ).fetchone()
    marker = await (
        await db.execute(
            "SELECT value FROM kv_settings WHERE key='dream_last_processed_message_id'"
        )
    ).fetchone()
    candidate = await (
        await db.execute("SELECT status FROM dream_candidate")
    ).fetchone()
    evidence_count = await (
        await db.execute("SELECT COUNT(*) FROM dream_evidence")
    ).fetchone()
    report = await (
        await db.execute(
            "SELECT run_id, candidates, promoted, dream_text FROM dream_report"
        )
    ).fetchone()
    reflection_row = await (
        await db.execute("SELECT kind, text FROM reflection")
    ).fetchone()
    assert tuple(run) == ("completed", 12)
    assert marker["value"] == "12"
    assert candidate["status"] == "applied"
    assert evidence_count[0] == 3
    assert tuple(report) == (
        result["run_id"],
        1,
        1,
        "Пользователь регулярно развивает свой основной проект Persona.",
    )
    assert tuple(reflection_row) == (
        "dream",
        "Пользователь регулярно развивает свой основной проект Persona.",
    )


@pytest.mark.asyncio
async def test_provider_failure_retries_without_advancing_cursor(
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _user(db)
    docs = [
        {
            "source": "chat:1",
            "text": "Я развиваю проект Persona каждый день",
            "message_ids": [10],
            "latest_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        }
    ]

    class FailedClient:
        async def complete_json(self, *_args, **_kwargs):
            raise RuntimeError("provider offline")

        async def complete(self, *_args, **_kwargs):
            raise RuntimeError("provider offline")

    async def quiet(_now):
        return True

    async def gather(_user_id, _cutoff, _last):
        return docs, {10}, set()

    monkeypatch.setattr(reflection, "_is_quiet", quiet)
    monkeypatch.setattr(reflection, "_gather_documents", gather)
    monkeypatch.setattr("app.llm.client.make_client", lambda **_kwargs: FailedClient())

    result = await reflection.run_dream_cycle(1)

    run = await (
        await db.execute("SELECT status, safe_cursor FROM dream_run")
    ).fetchone()
    marker = await (
        await db.execute(
            "SELECT value FROM kv_settings WHERE key='dream_last_processed_message_id'"
        )
    ).fetchone()
    assert result["status"] == "retry"
    assert tuple(run) == ("retry", 0)
    assert marker is None
