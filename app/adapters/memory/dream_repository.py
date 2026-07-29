"""SQLite implementation of the durable dream proposal ledger."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from app.adapters.projection.sqlite_repository import SqliteProjectionOutbox
from app.application.memory.ports import (
    DreamApplySummary,
    DreamCompletionReport,
    DreamRunLease,
)
from app.domains.memory.dream import (
    DreamCandidate,
    DreamEvidence,
    MemorySnapshot,
    PolicyDecision,
)
from app.storage.db import get_connection, write_transaction

_MAX_ERROR_CHARS = 2_000
_MAX_WORKER_CHARS = 128
_ALLOWED_MEMORY_KINDS = frozenset(
    {"fact", "preference", "person", "project", "reminder", "other"}
)


class DreamLeaseError(RuntimeError):
    """The caller no longer owns a live dream-run lease."""


class DreamLedgerConflict(RuntimeError):
    """An idempotency key was reused for different immutable content."""


class SqliteDreamLedger:
    async def acquire_run(
        self,
        *,
        user_id: int,
        idempotency_key: str,
        worker_id: str,
        input_cursor: int,
        config: dict[str, object],
        lease_seconds: int = 300,
    ) -> DreamRunLease:
        key = _bounded(idempotency_key, 160, "idempotency_key")
        worker = _bounded(worker_id, _MAX_WORKER_CHARS, "worker_id")
        lease_modifier = f"+{max(30, min(int(lease_seconds), 1800))} seconds"
        config_json = _json(config)
        async with write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO dream_run(
                    user_id, idempotency_key, input_cursor, safe_cursor, config_json
                ) VALUES(?,?,?,?,?)
                ON CONFLICT(user_id, idempotency_key) DO NOTHING
                """,
                (user_id, key, max(0, int(input_cursor)), max(0, int(input_cursor)), config_json),
            )
            cursor = await conn.execute(
                """
                SELECT id, user_id, status, worker_id, lease_until, attempt_count,
                       input_cursor, config_json
                  FROM dream_run
                 WHERE user_id=? AND idempotency_key=?
                """,
                (user_id, key),
            )
            row = await cursor.fetchone()
            if row is None:  # pragma: no cover - protected by INSERT/UNIQUE
                raise RuntimeError("dream run disappeared after insert")
            if int(row["input_cursor"]) != max(0, int(input_cursor)):
                raise DreamLedgerConflict("idempotency key has a different input cursor")
            if str(row["config_json"]) != config_json:
                raise DreamLedgerConflict("idempotency key has a different policy config")
            status = str(row["status"])
            run_id = int(row["id"])
            attempts = int(row["attempt_count"])
            if status in {"completed", "cancelled"}:
                return DreamRunLease(run_id, user_id, worker, False, status, attempts)
            if (
                status == "running"
                and row["worker_id"] == worker
                and row["lease_until"] is not None
            ):
                await conn.execute(
                    """
                    UPDATE dream_run
                       SET lease_until=datetime('now', ?), updated_at=datetime('now')
                     WHERE id=? AND status='running' AND worker_id=?
                    """,
                    (lease_modifier, run_id, worker),
                )
                return DreamRunLease(run_id, user_id, worker, True, status, attempts)

            claim = await conn.execute(
                """
                UPDATE dream_run
                   SET status='running', worker_id=?,
                       lease_until=datetime('now', ?),
                       attempt_count=attempt_count+1,
                       started_at=COALESCE(started_at, datetime('now')),
                       error=NULL, retry_at=NULL, updated_at=datetime('now')
                 WHERE id=?
                   AND (
                       status='pending'
                       OR (status='retry' AND (retry_at IS NULL OR retry_at <= datetime('now')))
                       OR (status='running' AND lease_until < datetime('now'))
                   )
                 RETURNING attempt_count
                """,
                (worker, lease_modifier, run_id),
            )
            claimed = await claim.fetchone()
            if claimed is None:
                return DreamRunLease(run_id, user_id, worker, False, status, attempts)
            attempts = int(claimed["attempt_count"])
            await _audit(conn, run_id, None, "run_acquired", {"attempt": attempts})
            return DreamRunLease(run_id, user_id, worker, True, "running", attempts)

    async def heartbeat(self, lease: DreamRunLease, *, lease_seconds: int = 300) -> None:
        lease_modifier = f"+{max(30, min(int(lease_seconds), 1800))} seconds"
        async with write_transaction() as conn:
            cursor = await conn.execute(
                """
                UPDATE dream_run
                   SET lease_until=datetime('now', ?), updated_at=datetime('now')
                 WHERE id=? AND user_id=? AND status='running' AND worker_id=?
                   AND lease_until >= datetime('now')
                """,
                (lease_modifier, lease.run_id, lease.user_id, lease.worker_id),
            )
            if cursor.rowcount != 1:
                raise DreamLeaseError("dream run lease was lost")

    async def store_proposals(
        self,
        lease: DreamRunLease,
        candidates: tuple[DreamCandidate, ...],
    ) -> tuple[DreamCandidate, ...]:
        async with write_transaction() as conn:
            await _assert_lease(conn, lease)
            for candidate in candidates:
                cursor = await conn.execute(
                    """
                    INSERT INTO dream_candidate(
                        run_id, candidate_key, text, kind, proposed_action,
                        target_memory_id, score, observed_count, source_count
                    ) VALUES(?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(run_id, candidate_key) DO NOTHING
                    """,
                    (
                        lease.run_id,
                        candidate.key,
                        candidate.text,
                        candidate.kind,
                        candidate.proposed_action,
                        candidate.target_memory_id,
                        float(candidate.score),
                        max(1, int(candidate.observed_count)),
                        max(1, int(candidate.source_count)),
                    ),
                )
                candidate_id = (
                    int(cursor.lastrowid)
                    if cursor.lastrowid is not None and cursor.rowcount == 1
                    else await _existing_candidate_id(conn, lease.run_id, candidate)
                )
                for evidence in candidate.evidence:
                    evidence_key = _evidence_key(evidence)
                    await conn.execute(
                        """
                        INSERT INTO dream_evidence(
                            candidate_id, evidence_key, source_kind, source_ref,
                            source_message_id, owner_attributed, content_hash,
                            excerpt, observed_at
                        ) VALUES(?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(candidate_id, evidence_key) DO NOTHING
                        """,
                        (
                            candidate_id,
                            evidence_key,
                            evidence.source_kind,
                            evidence.source_ref,
                            evidence.source_message_id,
                            1 if evidence.owner_attributed else 0,
                            evidence.content_hash,
                            evidence.excerpt,
                            evidence.observed_at,
                        ),
                    )
            await conn.execute(
                """
                UPDATE dream_run
                   SET candidates_count=(
                       SELECT COUNT(*) FROM dream_candidate WHERE run_id=?
                   ), updated_at=datetime('now')
                 WHERE id=?
                """,
                (lease.run_id, lease.run_id),
            )
            await _audit(conn, lease.run_id, None, "proposals_stored", {"count": len(candidates)})
        return await self._list_candidates(lease.run_id)

    async def list_memories(self, user_id: int) -> tuple[MemorySnapshot, ...]:
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT id, text, kind, pinned, valid_until
                  FROM user_memory
                 WHERE user_id=?
                 ORDER BY id
                """,
                (user_id,),
            )
            rows = await cursor.fetchall()
        return tuple(
            MemorySnapshot(
                id=int(row["id"]),
                text=str(row["text"]),
                kind=str(row["kind"]),
                pinned=bool(row["pinned"]),
                active=row["valid_until"] is None,
            )
            for row in rows
        )

    async def apply_decision(  # noqa: PLR0911, PLR0912, PLR0915 - transactional state machine
        self,
        lease: DreamRunLease,
        candidate: DreamCandidate,
        decision: PolicyDecision,
    ) -> str:
        if candidate.id is None:
            raise ValueError("stored candidate id is required")
        async with write_transaction() as conn:
            await _assert_lease(conn, lease)
            row = await _candidate_for_apply(conn, lease, candidate.id)
            status = str(row["status"])
            if status != "proposed":
                return "applied" if status == "applied" else status

            trusted = await conn.execute(
                """
                SELECT 1 FROM dream_evidence
                 WHERE candidate_id=? AND source_kind='owner_chat'
                   AND owner_attributed=1
                 LIMIT 1
                """,
                (candidate.id,),
            )
            if await trusted.fetchone() is None and decision.action in {"add", "update"}:
                decision = PolicyDecision("reject", "missing_trusted_owner_evidence")
            guard_reason = _guard_reason(row, decision)
            if guard_reason is not None:
                decision = PolicyDecision("reject", guard_reason)

            if decision.action == "reject":
                await _finish_candidate(conn, candidate.id, "rejected", decision.reason, None)
                await _revision(
                    conn,
                    lease.run_id,
                    candidate.id,
                    "reject",
                    None,
                    None,
                    {"reason": decision.reason},
                )
                await _audit(
                    conn,
                    lease.run_id,
                    candidate.id,
                    "candidate_rejected",
                    {"reason": decision.reason},
                )
                return "rejected"

            if decision.action == "noop":
                await _finish_candidate(
                    conn, candidate.id, "noop", decision.reason, decision.target_memory_id
                )
                await _revision(
                    conn,
                    lease.run_id,
                    candidate.id,
                    "noop",
                    decision.target_memory_id,
                    None,
                    {"reason": decision.reason},
                )
                return "noop"

            exact = await conn.execute(
                """
                SELECT id, text FROM user_memory
                 WHERE user_id=? AND valid_until IS NULL
                """,
                (lease.user_id,),
            )
            duplicate = next(
                (
                    item
                    for item in await exact.fetchall()
                    if str(item["text"]).casefold() == str(row["text"]).casefold()
                ),
                None,
            )
            if duplicate is not None:
                memory_id = int(duplicate["id"])
                await _finish_candidate(
                    conn, candidate.id, "noop", "exact_active_memory_exists", memory_id
                )
                await _revision(
                    conn,
                    lease.run_id,
                    candidate.id,
                    "noop",
                    memory_id,
                    None,
                    {"reason": "exact_active_memory_exists"},
                )
                return "noop"

            prior: dict[str, Any] | None = None
            target_id: int | None = None
            if decision.action == "update":
                target_id = decision.target_memory_id
                target_cursor = await conn.execute(
                    """
                    SELECT id, text, kind, pinned, valid_until, superseded_by
                      FROM user_memory
                     WHERE id=? AND user_id=? AND valid_until IS NULL
                    """,
                    (target_id, lease.user_id),
                )
                target = await target_cursor.fetchone()
                if target is None or bool(target["pinned"]):
                    reason = (
                        "pinned_memory_is_immutable"
                        if target is not None and bool(target["pinned"])
                        else "update_target_not_active"
                    )
                    await _finish_candidate(conn, candidate.id, "rejected", reason, None)
                    await _revision(
                        conn,
                        lease.run_id,
                        candidate.id,
                        "reject",
                        target_id,
                        _row_json(target),
                        {"reason": reason},
                    )
                    return "rejected"
                prior = _row_json(target)

            cap_cursor = await conn.execute(
                """
                SELECT COUNT(*) AS n FROM user_memory
                 WHERE user_id=? AND valid_until IS NULL
                """,
                (lease.user_id,),
            )
            cap_row = await cap_cursor.fetchone()
            if (
                decision.action == "add"
                and cap_row is not None
                and int(cap_row["n"]) >= 80
            ):
                await _finish_candidate(
                    conn,
                    candidate.id,
                    "rejected",
                    "automatic_memory_cap_reached",
                    None,
                )
                await _revision(
                    conn,
                    lease.run_id,
                    candidate.id,
                    "reject",
                    None,
                    prior,
                    {"reason": "automatic_memory_cap_reached"},
                )
                return "rejected"

            source_session_id = await _source_session_id(conn, candidate.id)
            salience = max(1.0, min(10.0, round(float(row["score"]) * 10.0, 1)))
            insert = await conn.execute(
                """
                INSERT INTO user_memory(
                    user_id, kind, text, pinned, source_session_id,
                    salience, importance_source
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    lease.user_id,
                    str(row["kind"]),
                    str(row["text"]),
                    0,
                    source_session_id,
                    salience,
                    "dream_policy",
                ),
            )
            if insert.lastrowid is None:  # pragma: no cover - SQLite invariant
                raise RuntimeError("memory INSERT returned no id")
            memory_id = int(insert.lastrowid)

            if decision.action == "update":
                update = await conn.execute(
                    """
                    UPDATE user_memory
                       SET valid_until=datetime('now'), superseded_by=?,
                           updated_at=datetime('now')
                     WHERE id=? AND user_id=? AND valid_until IS NULL AND pinned=0
                    """,
                    (memory_id, target_id, lease.user_id),
                )
                if update.rowcount != 1:
                    raise RuntimeError("memory update target changed during transaction")

            result = {
                "id": memory_id,
                "text": str(row["text"]),
                "kind": str(row["kind"]),
                "pinned": False,
            }
            await _finish_candidate(
                conn, candidate.id, "applied", decision.reason, memory_id
            )
            await _revision(
                conn,
                lease.run_id,
                candidate.id,
                decision.action,
                memory_id,
                prior,
                result,
            )
            await _audit(
                conn,
                lease.run_id,
                candidate.id,
                "candidate_applied",
                {"action": decision.action, "memory_id": memory_id},
            )
            return "applied"

    async def complete_run(
        self,
        lease: DreamRunLease,
        *,
        safe_cursor: int,
        summary: DreamApplySummary,
        report: DreamCompletionReport,
    ) -> None:
        async with write_transaction() as conn:
            await _assert_lease(conn, lease)
            impact_score = float(report.impact_score)
            if not math.isfinite(impact_score) or not 0.0 <= impact_score <= 1.0:
                raise ValueError("dream impact_score must be finite and in [0, 1]")
            dream_text = " ".join((report.dream_text or "").split())[:1200]
            source_ids = tuple(
                dict.fromkeys(
                    int(item)
                    for item in report.source_message_ids
                    if int(item) > 0
                )
            )[:30]
            if dream_text:
                await conn.execute(
                    """
                    INSERT INTO reflection(
                        user_id, kind, text, source_message_ids
                    ) VALUES(?, 'dream', ?, ?)
                    """,
                    (
                        lease.user_id,
                        dream_text,
                        json.dumps(source_ids) if source_ids else None,
                    ),
                )
            # The human-facing report is part of completion, not a best-effort
            # follow-up.  Any failure rolls back it, the reflection, cursor,
            # terminal state, and completion audit together.
            await conn.execute(
                """
                INSERT INTO dream_report(
                    run_id, user_id, candidates, promoted, consolidations,
                    conflicts, dream_text, impact_score
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    lease.run_id,
                    lease.user_id,
                    summary.candidates,
                    summary.applied,
                    max(0, int(report.consolidations)),
                    max(0, int(report.conflicts)),
                    dream_text or None,
                    impact_score,
                ),
            )
            # Projection intent is durable in the exact same transaction as
            # the applied revisions, report, cursor, and terminal run state.
            # Graph/embedding I/O is deliberately deferred to its own worker.
            projection_count = (
                await SqliteProjectionOutbox().enqueue_dream_run_in_transaction(
                    conn,
                    run_id=lease.run_id,
                    owner_user_id=lease.user_id,
                )
            )
            # Cursor and terminal run state are one commit.  A crash can never
            # leave the cursor ahead of a run that is still retryable.
            await conn.execute(
                """
                INSERT INTO kv_settings(key, value, updated_at)
                VALUES('dream_last_processed_message_id', ?, datetime('now'))
                ON CONFLICT(key) DO UPDATE SET
                    value=CASE
                        WHEN CAST(kv_settings.value AS INTEGER) < CAST(excluded.value AS INTEGER)
                        THEN excluded.value
                        ELSE kv_settings.value
                    END,
                    updated_at=datetime('now')
                """,
                (str(max(0, int(safe_cursor))),),
            )
            cursor = await conn.execute(
                """
                UPDATE dream_run
                   SET status='completed', safe_cursor=?, candidates_count=?,
                       applied_count=?, rejected_count=?, error=NULL,
                       worker_id=NULL, lease_until=NULL,
                       completed_at=datetime('now'), updated_at=datetime('now')
                 WHERE id=? AND user_id=? AND status='running' AND worker_id=?
                """,
                (
                    max(0, int(safe_cursor)),
                    summary.candidates,
                    summary.applied,
                    summary.rejected,
                    lease.run_id,
                    lease.user_id,
                    lease.worker_id,
                ),
            )
            if cursor.rowcount != 1:
                raise DreamLeaseError("dream run could not be completed")
            await _audit(
                conn,
                lease.run_id,
                None,
                "run_completed",
                {
                    "safe_cursor": safe_cursor,
                    "applied": summary.applied,
                    "rejected": summary.rejected,
                    "noops": summary.noops,
                    "report": True,
                    "projection_intents": projection_count,
                },
            )

    async def retry_run(
        self,
        lease: DreamRunLease,
        *,
        error: str,
        retry_seconds: int = 300,
        safe_cursor: int,
    ) -> None:
        retry_modifier = f"+{max(1, min(int(retry_seconds), 86400))} seconds"
        clean_error = str(error or "dream cycle failed")[:_MAX_ERROR_CHARS]
        async with write_transaction() as conn:
            await _assert_lease(conn, lease)
            await conn.execute(
                """
                UPDATE dream_run
                   SET status='retry', safe_cursor=?, error=?,
                       retry_at=datetime('now', ?), worker_id=NULL,
                       lease_until=NULL, updated_at=datetime('now')
                 WHERE id=? AND user_id=? AND status='running' AND worker_id=?
                """,
                (
                    max(0, int(safe_cursor)),
                    clean_error,
                    retry_modifier,
                    lease.run_id,
                    lease.user_id,
                    lease.worker_id,
                ),
            )
            await _audit(
                conn,
                lease.run_id,
                None,
                "run_retry_scheduled",
                {"error": clean_error, "safe_cursor": safe_cursor},
            )

    async def _list_candidates(self, run_id: int) -> tuple[DreamCandidate, ...]:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT * FROM dream_candidate WHERE run_id=? ORDER BY id",
                (run_id,),
            )
            rows = await cursor.fetchall()
            result: list[DreamCandidate] = []
            for row in rows:
                evidence_cursor = await conn.execute(
                    "SELECT * FROM dream_evidence WHERE candidate_id=? ORDER BY id",
                    (int(row["id"]),),
                )
                evidence_rows = await evidence_cursor.fetchall()
                result.append(
                    DreamCandidate(
                        id=int(row["id"]),
                        key=str(row["candidate_key"]),
                        text=str(row["text"]),
                        kind=str(row["kind"]),
                        proposed_action=str(row["proposed_action"]),  # type: ignore[arg-type]
                        target_memory_id=(
                            int(row["target_memory_id"])
                            if row["target_memory_id"] is not None
                            else None
                        ),
                        score=float(row["score"]),
                        observed_count=int(row["observed_count"]),
                        source_count=int(row["source_count"]),
                        evidence=tuple(_evidence_from_row(item) for item in evidence_rows),
                    )
                )
        return tuple(result)


async def _assert_lease(conn: Any, lease: DreamRunLease) -> None:
    cursor = await conn.execute(
        """
        SELECT 1 FROM dream_run
         WHERE id=? AND user_id=? AND status='running' AND worker_id=?
           AND lease_until >= datetime('now')
        """,
        (lease.run_id, lease.user_id, lease.worker_id),
    )
    if await cursor.fetchone() is None:
        raise DreamLeaseError("dream run lease was lost")


async def _existing_candidate_id(
    conn: Any,
    run_id: int,
    candidate: DreamCandidate,
) -> int:
    cursor = await conn.execute(
        "SELECT * FROM dream_candidate WHERE run_id=? AND candidate_key=?",
        (run_id, candidate.key),
    )
    row = await cursor.fetchone()
    if row is None:  # pragma: no cover
        raise RuntimeError("candidate disappeared after conflict")
    expected = (
        candidate.text,
        candidate.kind,
        candidate.proposed_action,
        candidate.target_memory_id,
        float(candidate.score),
        max(1, candidate.observed_count),
        max(1, candidate.source_count),
    )
    actual = (
        str(row["text"]),
        str(row["kind"]),
        str(row["proposed_action"]),
        row["target_memory_id"],
        float(row["score"]),
        int(row["observed_count"]),
        int(row["source_count"]),
    )
    if actual != expected:
        raise DreamLedgerConflict("candidate key was reused for different proposal content")
    return int(row["id"])


async def _candidate_for_apply(conn: Any, lease: DreamRunLease, candidate_id: int) -> Any:
    cursor = await conn.execute(
        """
        SELECT c.*, r.config_json FROM dream_candidate c
        JOIN dream_run r ON r.id=c.run_id
        WHERE c.id=? AND c.run_id=? AND r.user_id=?
        """,
        (candidate_id, lease.run_id, lease.user_id),
    )
    row = await cursor.fetchone()
    if row is None:
        raise DreamLedgerConflict("candidate does not belong to leased run")
    return row


def _guard_reason(  # noqa: PLR0911 - explicit fail-closed guards
    row: Any, decision: PolicyDecision
) -> str | None:
    if decision.action not in {"add", "update"}:
        return None
    if str(row["kind"]) not in _ALLOWED_MEMORY_KINDS:
        return "unsupported_memory_kind"
    proposed_action = str(row["proposed_action"])
    if decision.action != proposed_action:
        return "policy_action_does_not_match_proposal"
    if decision.action == "update":
        stored_target = (
            int(row["target_memory_id"]) if row["target_memory_id"] is not None else None
        )
        if decision.target_memory_id != stored_target:
            return "policy_target_does_not_match_proposal"
    try:
        config = json.loads(str(row["config_json"]))
        threshold = float(config.get("promotion_threshold", config.get("threshold", 0.6)))
        min_recall = max(1, int(config.get("min_recall_count", 2)))
    except (TypeError, ValueError, json.JSONDecodeError):
        return "invalid_run_policy_config"
    if float(row["score"]) <= threshold:
        return "score_below_threshold"
    if int(row["source_count"]) < min_recall:
        return "insufficient_source_diversity"
    return None


async def _finish_candidate(
    conn: Any,
    candidate_id: int,
    status: str,
    reason: str,
    result_memory_id: int | None,
) -> None:
    await conn.execute(
        """
        UPDATE dream_candidate
           SET status=?, policy_reason=?, result_memory_id=?,
               decided_at=datetime('now')
         WHERE id=? AND status='proposed'
        """,
        (status, reason, result_memory_id, candidate_id),
    )


async def _source_session_id(conn: Any, candidate_id: int) -> int | None:
    cursor = await conn.execute(
        """
        SELECT source_ref FROM dream_evidence
         WHERE candidate_id=? AND source_kind='owner_chat'
           AND owner_attributed=1 AND source_ref LIKE 'chat:%'
         ORDER BY id LIMIT 1
        """,
        (candidate_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    try:
        return int(str(row["source_ref"]).split(":", 1)[1])
    except (IndexError, ValueError):
        return None


async def _revision(
    conn: Any,
    run_id: int,
    candidate_id: int,
    action: str,
    memory_id: int | None,
    prior: dict[str, Any] | None,
    result: dict[str, Any] | None,
) -> None:
    await conn.execute(
        """
        INSERT INTO dream_revision(
            run_id, candidate_id, action, memory_id, prior_json, result_json
        ) VALUES(?,?,?,?,?,?)
        """,
        (
            run_id,
            candidate_id,
            action,
            memory_id,
            _json(prior) if prior is not None else None,
            _json(result) if result is not None else None,
        ),
    )


async def _audit(
    conn: Any,
    run_id: int,
    candidate_id: int | None,
    event: str,
    detail: dict[str, Any],
) -> None:
    await conn.execute(
        "INSERT INTO dream_audit(run_id, candidate_id, event, detail_json) VALUES(?,?,?,?)",
        (run_id, candidate_id, event, _json(detail)),
    )


def _evidence_key(evidence: DreamEvidence) -> str:
    value = "|".join(
        (
            evidence.source_kind,
            evidence.source_ref,
            str(evidence.source_message_id or ""),
            evidence.content_hash,
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _evidence_from_row(row: Any) -> DreamEvidence:
    return DreamEvidence(
        source_kind=str(row["source_kind"]),
        source_ref=str(row["source_ref"]),
        source_message_id=(
            int(row["source_message_id"]) if row["source_message_id"] is not None else None
        ),
        owner_attributed=bool(row["owner_attributed"]),
        content_hash=str(row["content_hash"]),
        excerpt=str(row["excerpt"]) if row["excerpt"] is not None else None,
        observed_at=str(row["observed_at"]) if row["observed_at"] is not None else None,
    )


def _row_json(row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _bounded(value: str, limit: int, name: str) -> str:
    clean = str(value or "").strip()
    if not clean or len(clean) > limit:
        raise ValueError(f"{name} must contain 1..{limit} characters")
    return clean


__all__ = [
    "DreamLeaseError",
    "DreamLedgerConflict",
    "SqliteDreamLedger",
]
