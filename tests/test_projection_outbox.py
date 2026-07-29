"""Durability, privacy, and idempotency contracts for memory projections."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import aiosqlite
import pytest

from app.adapters.memory.dream_repository import SqliteDreamLedger
from app.adapters.projection import SqliteProjectionOutbox
from app.adapters.projection import sqlite_repository as projection_repository
from app.adapters.projection.sqlite_repository import ProjectionLeaseError
from app.application.memory.dream_service import DreamLedgerService
from app.application.memory.ports import DreamCompletionReport
from app.application.projection import ProjectionDispatcher
from app.application.projection.ports import ProjectionCapabilityUnavailable
from app.auth.roles import delete_user
from app.domains.memory.dream import DreamCandidate, DreamEvidence, DreamPolicy
from app.domains.projection import (
    GraphProjection,
    GraphTriple,
    ProjectionEvidence,
    ProjectionKind,
    ProjectionPolicy,
    ProjectionSource,
)
from app.storage.db import write_transaction
from app.workers import projection_worker
from app.workers.heartbeat import beat

if TYPE_CHECKING:
    from app.application.memory.ports import DreamApplySummary, DreamRunLease
    from app.domains.projection import ProjectionJob


async def _user(db: aiosqlite.Connection, user_id: int = 1) -> None:
    await db.execute(
        "INSERT INTO users(id, email, password_hash) VALUES(?,?,?)",
        (user_id, f"{user_id}@projection.test", "x"),
    )
    await db.commit()


def _candidate(
    text: str = "Пользователь ежедневно развивает проект Persona",
    *,
    source_kind: str = "owner_chat",
    owner_attributed: bool = True,
    excerpt: str = "Пользователь развивает проект Persona",
) -> DreamCandidate:
    ref = f"{source_kind}:42:{text}"
    evidence = DreamEvidence(
        source_kind=source_kind,  # type: ignore[arg-type]
        source_ref=ref,
        source_message_id=42,
        owner_attributed=owner_attributed,
        content_hash=hashlib.sha256(ref.encode()).hexdigest(),
        excerpt=excerpt,
        observed_at="2026-07-29 01:00:00",
    )
    return DreamCandidate(
        key=hashlib.sha256(text.encode()).hexdigest(),
        text=text,
        kind="project",
        proposed_action="add",
        target_memory_id=None,
        score=0.95,
        observed_count=2,
        source_count=1,
        evidence=(evidence,),
    )


async def _applied_run(
    db: aiosqlite.Connection,
    *,
    candidate: DreamCandidate | None = None,
    key: str = "projection-run",
) -> tuple[SqliteDreamLedger, DreamRunLease, DreamApplySummary]:
    await _user(db)
    ledger = SqliteDreamLedger()
    lease = await ledger.acquire_run(
        user_id=1,
        idempotency_key=key,
        worker_id="dream:test",
        input_cursor=10,
        config={"threshold": 0.6, "min_recall_count": 1},
        lease_seconds=120,
    )
    summary = await DreamLedgerService(ledger).apply_proposals(
        lease,
        (candidate or _candidate(),),
        DreamPolicy(score_threshold=0.6, min_recall_count=1),
    )
    return ledger, lease, summary


async def _complete(
    ledger: SqliteDreamLedger,
    lease: DreamRunLease,
    summary: DreamApplySummary,
) -> None:
    await ledger.complete_run(
        lease,
        safe_cursor=42,
        summary=summary,
        report=DreamCompletionReport(
            dream_text="Ночная консолидация памяти завершена",
            source_message_ids=(42,),
            impact_score=0.8,
        ),
    )


def test_projection_policy_rejects_russian_secret_material() -> None:
    source = ProjectionSource(
        owner_user_id=1,
        dream_revision_id=1,
        memory_id=1,
        text="Пароль: сверхсекретный-пароль",
        content_hash="hash",
        revision_action="add",
        candidate_status="applied",
        memory_pinned=False,
        memory_active=True,
        evidence=(
            ProjectionEvidence(
                id=1,
                source_kind="owner_chat",
                owner_attributed=True,
                content_hash="evidence-hash",
            ),
        ),
    )

    decision = ProjectionPolicy().decide(source)

    assert not decision.eligible
    assert decision.reason == "secret_material"


@pytest.mark.parametrize(
    "secret",
    (
        "ghp_0123456789ABCDEFabcdef0123456789",
        "sk-proj-AbCDef0123456789_-AbCDef0123456789",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk",
        "Ab9_dE2+qL7.mN4~vX8/Cz1=Rt5-Yu3_Po6",
    ),
)
def test_projection_policy_rejects_unlabelled_credential_material(
    secret: str,
) -> None:
    source = ProjectionSource(
        owner_user_id=1,
        dream_revision_id=1,
        memory_id=1,
        text=f"Случайная строка из переписки {secret}",
        content_hash="hash",
        revision_action="add",
        candidate_status="applied",
        memory_pinned=False,
        memory_active=True,
        evidence=(
            ProjectionEvidence(
                id=1,
                source_kind="owner_chat",
                owner_attributed=True,
                content_hash="evidence-hash",
            ),
        ),
    )

    decision = ProjectionPolicy().decide(source)

    assert not decision.eligible
    assert decision.reason == "secret_material"


def test_projection_policy_does_not_treat_hex_content_hash_as_secret() -> None:
    source = ProjectionSource(
        owner_user_id=1,
        dream_revision_id=1,
        memory_id=1,
        text="Релиз использует commit 0123456789abcdef0123456789abcdef01234567",
        content_hash="hash",
        revision_action="add",
        candidate_status="applied",
        memory_pinned=False,
        memory_active=True,
        evidence=(
            ProjectionEvidence(
                id=1,
                source_kind="owner_chat",
                owner_attributed=True,
                content_hash="evidence-hash",
            ),
        ),
    )

    assert ProjectionPolicy().decide(source).eligible


@pytest.mark.asyncio
async def test_completion_and_projection_intents_roll_back_together(
    db: aiosqlite.Connection,
) -> None:
    ledger, lease, summary = await _applied_run(db)
    await db.execute(
        """
        CREATE TRIGGER fail_projection_completion
        BEFORE UPDATE ON dream_run
        WHEN NEW.status='completed'
        BEGIN
            SELECT RAISE(ABORT, 'simulated terminal failure');
        END
        """
    )
    await db.commit()

    with pytest.raises(aiosqlite.IntegrityError, match="simulated terminal failure"):
        await _complete(ledger, lease, summary)

    run = await (
        await db.execute("SELECT status, safe_cursor FROM dream_run WHERE id=?", (lease.run_id,))
    ).fetchone()
    counts = {}
    for table in ("memory_projection_outbox", "dream_report", "reflection"):
        row = await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
        counts[table] = int(row[0])
    marker = await (
        await db.execute(
            "SELECT value FROM kv_settings WHERE key='dream_last_processed_message_id'"
        )
    ).fetchone()

    assert tuple(run) == ("running", 10)
    assert marker is None
    assert counts == {
        "memory_projection_outbox": 0,
        "dream_report": 0,
        "reflection": 0,
    }


@pytest.mark.asyncio
async def test_completion_enqueue_is_evidence_linked_and_idempotent(
    db: aiosqlite.Connection,
) -> None:
    ledger, lease, summary = await _applied_run(db)
    await _complete(ledger, lease, summary)
    outbox = SqliteProjectionOutbox()

    async with write_transaction() as conn:
        first_retry = await outbox.enqueue_dream_run_in_transaction(
            conn,
            run_id=lease.run_id,
            owner_user_id=lease.user_id,
        )
        second_retry = await outbox.enqueue_dream_run_in_transaction(
            conn,
            run_id=lease.run_id,
            owner_user_id=lease.user_id,
        )

    rows = await (
        await db.execute(
            """
            SELECT projection_kind, status, owner_user_id, dream_revision_id,
                   memory_id, content_hash
              FROM memory_projection_outbox
             ORDER BY id
            """
        )
    ).fetchall()
    evidence_links = await (
        await db.execute("SELECT COUNT(*) FROM memory_projection_evidence")
    ).fetchone()

    assert first_retry == second_retry == 0
    assert [row["projection_kind"] for row in rows] == ["graph", "embedding"]
    assert all(row["status"] == "pending" and row["owner_user_id"] == 1 for row in rows)
    assert rows[0]["dream_revision_id"] == rows[1]["dream_revision_id"]
    assert rows[0]["memory_id"] == rows[1]["memory_id"]
    assert rows[0]["content_hash"] == rows[1]["content_hash"]
    assert evidence_links[0] == 2


@pytest.mark.asyncio
async def test_storage_rejects_cross_owner_projection_rows(
    db: aiosqlite.Connection,
) -> None:
    ledger, lease, summary = await _applied_run(db)
    await _complete(ledger, lease, summary)
    await _user(db, user_id=2)
    row = await (
        await db.execute(
            """
            SELECT dream_revision_id, memory_id, content_hash
              FROM memory_projection_outbox LIMIT 1
            """
        )
    ).fetchone()

    with pytest.raises(aiosqlite.IntegrityError, match="owner/source mismatch"):
        await db.execute(
            """
            INSERT INTO memory_projection_outbox(
                owner_user_id, dream_revision_id, memory_id,
                projection_kind, projector_version, content_hash
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                2,
                int(row["dream_revision_id"]),
                int(row["memory_id"]),
                "graph",
                99,
                str(row["content_hash"]),
            ),
        )
    await db.rollback()


@pytest.mark.asyncio
async def test_empty_claim_polls_never_open_a_write_transaction(
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    @asynccontextmanager
    async def forbidden_write_transaction(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("empty projection poll acquired a writer lock")
        yield  # pragma: no cover

    monkeypatch.setattr(
        projection_repository,
        "write_transaction",
        forbidden_write_transaction,
    )
    outbox = SqliteProjectionOutbox()
    for index in range(3):
        assert (
            await outbox.claim(
                expected_owner_user_id=1,
                lease_owner=f"empty-worker-{index}",
                now=datetime.now(UTC),
            )
            is None
        )

    assert calls == 0


@pytest.mark.asyncio
async def test_empty_worker_polls_do_not_write_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stop = asyncio.Event()
    polls = 0
    heartbeat_calls = 0

    async def owner() -> int:
        return 1

    async def heartbeat(_name: str, _status: str) -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1

    class EmptyDispatcher:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        async def run_once(self, *, now: datetime) -> bool:
            nonlocal polls
            polls += 1
            if polls == 3:
                stop.set()
            return False

    monkeypatch.setattr(projection_worker, "get_owner_user_id", owner)
    monkeypatch.setattr(projection_worker, "beat", heartbeat)
    monkeypatch.setattr(projection_worker, "ProjectionDispatcher", EmptyDispatcher)

    await projection_worker.run_memory_projection_worker(
        stop,
        poll_seconds=0.25,
    )

    assert polls == 3
    assert heartbeat_calls == 0


@pytest.mark.asyncio
async def test_concurrent_claim_after_read_preflight_has_one_winner(
    db: aiosqlite.Connection,
) -> None:
    ledger, lease, summary = await _applied_run(db)
    await _complete(ledger, lease, summary)
    await db.execute(
        """
        UPDATE memory_projection_outbox
           SET due_at=datetime('now', '+1 day')
         WHERE projection_kind='embedding'
        """
    )
    await db.commit()
    outbox = SqliteProjectionOutbox()
    now = datetime.now(UTC)

    results = await asyncio.gather(
        outbox.claim(
            expected_owner_user_id=1,
            lease_owner="concurrent-a",
            now=now,
        ),
        outbox.claim(
            expected_owner_user_id=1,
            lease_owner="concurrent-b",
            now=now,
        ),
    )

    winners = [job for job in results if job is not None]
    assert len(winners) == 1
    assert winners[0].kind is ProjectionKind.GRAPH


@pytest.mark.asyncio
async def test_claim_query_uses_owner_due_index_without_temp_sort(
    db: aiosqlite.Connection,
) -> None:
    plan = await (
        await db.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT o.*, r.action AS revision_action,
                   c.id AS candidate_id, c.status AS candidate_status,
                   m.text, m.pinned, m.valid_until
              FROM memory_projection_outbox o
              JOIN dream_revision r ON r.id=o.dream_revision_id
              JOIN dream_run run
                ON run.id=r.run_id AND run.user_id=o.owner_user_id
              JOIN dream_candidate c ON c.id=r.candidate_id
              JOIN user_memory m
                ON m.id=o.memory_id AND m.id=r.memory_id
               AND m.user_id=o.owner_user_id
             WHERE o.owner_user_id=?
               AND o.status IN ('pending', 'retry')
               AND o.due_at <= ?
             ORDER BY o.due_at, o.id
             LIMIT ?
            """,
            (1, "2099-01-01 00:00:00", 50),
        )
    ).fetchall()
    details = [str(row["detail"]) for row in plan]

    assert any("idx_memory_projection_owner_due" in detail for detail in details)
    assert not any("TEMP B-TREE" in detail for detail in details)


@pytest.mark.asyncio
async def test_lease_is_owner_scoped_and_expired_job_is_safely_reclaimed(
    db: aiosqlite.Connection,
) -> None:
    ledger, lease, summary = await _applied_run(db)
    await _complete(ledger, lease, summary)
    await db.execute(
        """
        UPDATE memory_projection_outbox
           SET due_at=datetime('now', '+1 day')
         WHERE projection_kind='embedding'
        """
    )
    await db.commit()
    outbox = SqliteProjectionOutbox()
    now = datetime.now(UTC)

    assert (
        await outbox.claim(
            expected_owner_user_id=2,
            lease_owner="other-owner-worker",
            now=now,
        )
        is None
    )
    first = await outbox.claim(
        expected_owner_user_id=1,
        lease_owner="worker-a",
        now=now,
        lease_seconds=30,
    )
    assert first is not None
    assert first.kind is ProjectionKind.GRAPH
    assert first.attempts == 1
    assert (
        await outbox.claim(
            expected_owner_user_id=1,
            lease_owner="worker-b",
            now=now,
        )
        is None
    )

    await db.execute(
        "UPDATE memory_projection_outbox SET lease_until=datetime('now', '-1 second') WHERE id=?",
        (first.id,),
    )
    await db.commit()
    reclaimed = await outbox.claim(
        expected_owner_user_id=1,
        lease_owner="worker-b",
        now=now + timedelta(seconds=31),
    )
    assert reclaimed is not None
    assert reclaimed.id == first.id
    assert reclaimed.attempts == 2

    with pytest.raises(ProjectionLeaseError):
        await outbox.complete(first, GraphProjection(triples=()), now=now)


@pytest.mark.asyncio
async def test_retry_becomes_dead_letter_and_updates_capability(
    db: aiosqlite.Connection,
) -> None:
    ledger, lease, summary = await _applied_run(db)
    await _complete(ledger, lease, summary)
    await db.execute(
        """
        UPDATE memory_projection_outbox
           SET max_attempts=2,
               due_at=CASE WHEN projection_kind='embedding'
                           THEN datetime('now', '+1 day') ELSE due_at END
        """
    )
    await db.commit()

    class OfflineGraph:
        kind = ProjectionKind.GRAPH

        async def project(self, job: ProjectionJob) -> GraphProjection:
            raise ProjectionCapabilityUnavailable(
                "graph_provider_unavailable",
                unavailable=True,
            )

    outbox = SqliteProjectionOutbox()
    dispatcher = ProjectionDispatcher(
        outbox,
        {ProjectionKind.GRAPH: OfflineGraph()},
        expected_owner_user_id=1,
        lease_owner="retry-worker",
        lease_seconds=30,
    )
    now = datetime.now(UTC)
    assert await dispatcher.run_once(now=now)
    await db.execute(
        """
        UPDATE memory_projection_outbox SET due_at=datetime('now', '-1 second')
         WHERE projection_kind='graph'
        """
    )
    await db.commit()
    assert await dispatcher.run_once(now=now + timedelta(minutes=1))

    row = await (
        await db.execute(
            """
            SELECT status, attempts, last_error_code
              FROM memory_projection_outbox
             WHERE projection_kind='graph'
            """
        )
    ).fetchone()
    capability = await (
        await db.execute(
            """
            SELECT status, failures, detail_code
              FROM memory_projection_capability WHERE name='graph'
            """
        )
    ).fetchone()
    assert tuple(row) == ("dead", 2, "graph_provider_unavailable")
    assert tuple(capability) == ("unavailable", 2, "graph_provider_unavailable")


@pytest.mark.asyncio
async def test_projector_io_has_no_db_transaction_and_completion_rechecks_policy(
    db: aiosqlite.Connection,
) -> None:
    ledger, lease, summary = await _applied_run(db)
    await _complete(ledger, lease, summary)
    await db.execute(
        """
        UPDATE memory_projection_outbox
           SET due_at=datetime('now', '+1 day')
         WHERE projection_kind='embedding'
        """
    )
    await db.commit()

    class PinDuringProjection:
        kind = ProjectionKind.GRAPH

        async def project(self, job: ProjectionJob) -> GraphProjection:
            # This write transaction would fail/block if claim retained its
            # SQLite transaction across model/graph I/O.
            async with write_transaction() as conn:
                await conn.execute(
                    "UPDATE user_memory SET pinned=1 WHERE id=?",
                    (job.source.memory_id,),
                )
            return GraphProjection(
                triples=(GraphTriple("Ярослав", "развивает", "Persona"),)
            )

    dispatcher = ProjectionDispatcher(
        SqliteProjectionOutbox(),
        {ProjectionKind.GRAPH: PinDuringProjection()},
        expected_owner_user_id=1,
        lease_owner="transaction-probe",
    )

    assert await dispatcher.run_once(now=datetime.now(UTC))
    row = await (
        await db.execute(
            """
            SELECT status, last_error_code
              FROM memory_projection_outbox
             WHERE projection_kind='graph'
            """
        )
    ).fetchone()
    edge_count = await (await db.execute("SELECT COUNT(*) FROM kg_edge")).fetchone()
    assert tuple(row) == ("cancelled", "memory_pinned")
    assert edge_count[0] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["group", "secret", "pinned", "invalid"])
async def test_ineligible_changes_never_enqueue(
    db: aiosqlite.Connection,
    case: str,
) -> None:
    candidate = (
        _candidate(
            source_kind="telegram_group",
            owner_attributed=False,
        )
        if case == "group"
        else _candidate(
            "Пароль: qwerty-сверхсекретный"
            if case == "secret"
            else "Пользователь развивает приватный проект Persona"
        )
    )
    ledger, lease, summary = await _applied_run(db, candidate=candidate)
    if case == "pinned":
        await db.execute("UPDATE user_memory SET pinned=1")
        await db.commit()
    elif case == "invalid":
        await db.execute("UPDATE user_memory SET valid_until=datetime('now')")
        await db.commit()

    await _complete(ledger, lease, summary)

    count = await (
        await db.execute("SELECT COUNT(*) FROM memory_projection_outbox")
    ).fetchone()
    assert count[0] == 0


@pytest.mark.asyncio
async def test_real_gateways_persist_idempotent_graph_and_embedding(
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, lease, summary = await _applied_run(db)
    await _complete(ledger, lease, summary)

    async def triples(_text: str) -> list[dict[str, str]]:
        return [
            {
                "subject": "Ярослав",
                "relation": "развивает",
                "object": "Persona",
            }
        ]

    async def embedding(_text: str, kind: str = "document") -> list[float]:
        assert kind == "document"
        return [0.25, -0.5, 0.75]

    async def model_name() -> str:
        return "test-embedding"

    monkeypatch.setattr("app.knowledge_graph.extract_projection_triples", triples)
    monkeypatch.setattr("app.memory_vec.embed", embedding)
    monkeypatch.setattr("app.memory_vec.embedding_model_name", model_name)

    from app.adapters.projection import (  # noqa: PLC0415
        ExistingEmbeddingGateway,
        ExistingGraphGateway,
    )

    outbox = SqliteProjectionOutbox()
    dispatcher = ProjectionDispatcher(
        outbox,
        {
            ProjectionKind.GRAPH: ExistingGraphGateway(),
            ProjectionKind.EMBEDDING: ExistingEmbeddingGateway(),
        },
        expected_owner_user_id=1,
        lease_owner="real-adapters",
    )
    now = datetime.now(UTC)
    assert await dispatcher.run_once(now=now)
    assert await dispatcher.run_once(now=now)
    assert not await dispatcher.run_once(now=now)

    revision = await (
        await db.execute("SELECT id FROM dream_revision")
    ).fetchone()
    from app.knowledge_graph import (  # noqa: PLC0415
        store_projection_triples_in_transaction,
    )

    async with write_transaction() as conn:
        duplicate_units = await store_projection_triples_in_transaction(
            conn,
            user_id=1,
            dream_revision_id=int(revision["id"]),
            triples=await triples("ignored"),
        )

    statuses = await (
        await db.execute(
            "SELECT projection_kind, status FROM memory_projection_outbox ORDER BY id"
        )
    ).fetchall()
    edge = await (
        await db.execute("SELECT strength, source_kind FROM kg_edge")
    ).fetchone()
    graph_link_count = await (
        await db.execute("SELECT COUNT(*) FROM graph_revision_projection")
    ).fetchone()
    vector = await (
        await db.execute(
            "SELECT model_name, dimensions, length(embedding) AS bytes "
            "FROM memory_revision_embedding"
        )
    ).fetchone()
    capability = await (
        await db.execute(
            "SELECT name, status, successes FROM memory_projection_capability ORDER BY name"
        )
    ).fetchall()

    assert [tuple(row) for row in statuses] == [
        ("graph", "done"),
        ("embedding", "done"),
    ]
    assert duplicate_units == 0
    assert tuple(edge) == (1.0, "dream_revision")
    assert graph_link_count[0] == 1
    assert tuple(vector) == ("test-embedding", 3, 12)
    assert [tuple(row) for row in capability] == [
        ("embedding", "ready", 1),
        ("graph", "ready", 1),
    ]


@pytest.mark.asyncio
async def test_update_revision_retires_only_graph_edges_without_active_support(
    db: aiosqlite.Connection,
) -> None:
    ledger, first_lease, first_summary = await _applied_run(db)
    await _complete(ledger, first_lease, first_summary)
    old_memory = await (
        await db.execute(
            "SELECT id FROM user_memory WHERE user_id=1 AND valid_until IS NULL"
        )
    ).fetchone()
    first_revision = await (
        await db.execute("SELECT id FROM dream_revision WHERE action='add'")
    ).fetchone()
    from app.knowledge_graph import (  # noqa: PLC0415
        store_projection_triples_in_transaction,
    )

    shared = {
        "subject": "Ярослав",
        "relation": "использует",
        "object": "Python",
    }
    async with write_transaction() as conn:
        await store_projection_triples_in_transaction(
            conn,
            user_id=1,
            dream_revision_id=int(first_revision["id"]),
            triples=[
                shared,
                {
                    "subject": "Ярослав",
                    "relation": "развивает",
                    "object": "Persona",
                },
            ],
        )

    second_lease = await ledger.acquire_run(
        user_id=1,
        idempotency_key="projection-update-run",
        worker_id="dream:test:update",
        input_cursor=42,
        config={"threshold": 0.6, "min_recall_count": 1},
        lease_seconds=120,
    )
    update_candidate = replace(
        _candidate("Пользователь ежедневно развивает проект Hermes"),
        proposed_action="update",
        target_memory_id=int(old_memory["id"]),
    )
    second_summary = await DreamLedgerService(ledger).apply_proposals(
        second_lease,
        (update_candidate,),
        DreamPolicy(score_threshold=0.6, min_recall_count=1),
    )
    await _complete(ledger, second_lease, second_summary)

    retired_before_provider = await (
        await db.execute(
            """
            SELECT COUNT(*) AS n FROM kg_edge
             WHERE user_id=1 AND valid_until IS NULL
            """
        )
    ).fetchone()
    assert retired_before_provider["n"] == 0

    revisions = await (
        await db.execute(
            "SELECT id, action FROM dream_revision ORDER BY id"
        )
    ).fetchall()
    assert [row["action"] for row in revisions] == ["add", "update"]

    async with write_transaction() as conn:
        await store_projection_triples_in_transaction(
            conn,
            user_id=1,
            dream_revision_id=int(revisions[1]["id"]),
            triples=[
                shared,
                {
                    "subject": "Ярослав",
                    "relation": "развивает",
                    "object": "Hermes",
                },
            ],
        )

    edges = await (
        await db.execute(
            """
            SELECT target.name AS object_name, edge.strength, edge.valid_until
              FROM kg_edge edge
             JOIN kg_entity target ON target.id=edge.to_entity_id
             WHERE edge.user_id=1
             ORDER BY target.name, edge.id
            """
        )
    ).fetchall()
    by_object = {str(row["object_name"]): row for row in edges}
    assert by_object["Persona"]["valid_until"] is not None
    assert by_object["Hermes"]["valid_until"] is None
    assert by_object["Python"]["valid_until"] is None
    assert by_object["Python"]["strength"] == 1.0


@pytest.mark.asyncio
async def test_user_delete_purges_every_projection_and_legacy_graph_row(
    db: aiosqlite.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, lease, summary = await _applied_run(db)
    await _complete(ledger, lease, summary)

    async def triples(_text: str) -> list[dict[str, str]]:
        return [{"subject": "Ярослав", "relation": "создал", "object": "Persona"}]

    async def embedding(_text: str, kind: str = "document") -> list[float]:
        return [0.1, 0.2]

    async def model_name() -> str:
        return "privacy-test"

    monkeypatch.setattr("app.knowledge_graph.extract_projection_triples", triples)
    monkeypatch.setattr("app.memory_vec.embed", embedding)
    monkeypatch.setattr("app.memory_vec.embedding_model_name", model_name)

    from app.adapters.projection import (  # noqa: PLC0415
        ExistingEmbeddingGateway,
        ExistingGraphGateway,
    )

    dispatcher = ProjectionDispatcher(
        SqliteProjectionOutbox(),
        {
            ProjectionKind.GRAPH: ExistingGraphGateway(),
            ProjectionKind.EMBEDDING: ExistingEmbeddingGateway(),
        },
        expected_owner_user_id=1,
        lease_owner="privacy-worker",
    )
    now = datetime.now(UTC)
    assert await dispatcher.run_once(now=now)
    assert await dispatcher.run_once(now=now)
    await beat("memory-projection", "projected")

    health = await SqliteProjectionOutbox().health_status()
    assert health["counts"] == {"done": 2}
    assert health["heartbeat"]["last_status"] == "projected"

    assert await delete_user(1) is True
    for table in (
        "memory_projection_evidence",
        "memory_revision_embedding",
        "graph_revision_projection",
        "memory_projection_outbox",
        "kg_edge",
        "kg_entity",
    ):
        count = await (await db.execute(f"SELECT COUNT(*) FROM {table}")).fetchone()
        assert count[0] == 0, table
