"""SQLite outbox for evidence-linked graph and embedding projection."""

from __future__ import annotations

import hashlib
import math
import re
import struct
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

from app.domains.projection import (
    EmbeddingProjection,
    GraphProjection,
    ProjectionEvidence,
    ProjectionJob,
    ProjectionKind,
    ProjectionPolicy,
    ProjectionSource,
)
from app.storage.db import get_connection, write_transaction

if TYPE_CHECKING:
    from app.domains.projection.model import ProjectionPayload

_LEASE_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_MAX_CLAIM_SCAN: Final[int] = 50
_PROJECTOR_VERSION: Final[int] = 1
_HEARTBEAT_NAME: Final[str] = "memory-projection"
_MAX_EMBEDDING_DIMENSIONS: Final[int] = 4096


class ProjectionLeaseError(RuntimeError):
    """Outbox job is no longer leased by this worker."""


class ProjectionPayloadError(ValueError):
    """Projector returned a payload for the wrong job kind."""


class SqliteProjectionOutbox:
    def __init__(self, *, policy: ProjectionPolicy | None = None) -> None:
        self._policy = policy or ProjectionPolicy()

    async def enqueue_dream_run_in_transaction(
        self,
        conn: Any,
        *,
        run_id: int,
        owner_user_id: int,
    ) -> int:
        """Enqueue eligible applied revisions on the caller's completion transaction."""
        cursor = await conn.execute(
            """
            SELECT r.id AS revision_id, r.action AS revision_action,
                   r.memory_id, c.id AS candidate_id, c.status AS candidate_status,
                   m.text, m.pinned, m.valid_until
              FROM dream_revision r
              JOIN dream_candidate c ON c.id=r.candidate_id
              JOIN dream_run run ON run.id=r.run_id
              JOIN user_memory m ON m.id=r.memory_id AND m.user_id=run.user_id
             WHERE r.run_id=? AND run.user_id=?
             ORDER BY r.id
            """,
            (run_id, owner_user_id),
        )
        rows = await cursor.fetchall()
        inserted = 0
        for row in rows:
            source = await _source_from_revision_row(
                conn,
                owner_user_id=owner_user_id,
                row=row,
            )
            if source.revision_action == "update":
                from app.knowledge_graph import (  # noqa: PLC0415
                    reconcile_superseded_projection_edges_in_transaction,
                )

                # This is deterministic lineage cleanup, not model/network
                # projection. Run it in dream completion so a provider outage
                # or dead-letter cannot leave the superseded graph current.
                await reconcile_superseded_projection_edges_in_transaction(
                    conn,
                    user_id=owner_user_id,
                    dream_revision_id=source.dream_revision_id,
                )
            if not self._policy.decide(source).eligible:
                continue
            trusted_evidence = tuple(
                item for item in source.evidence if item.trusted_owner_chat
            )
            for kind in ProjectionKind:
                outbox_cursor = await conn.execute(
                    """
                    INSERT INTO memory_projection_outbox(
                        owner_user_id, dream_revision_id, memory_id,
                        projection_kind, projector_version, content_hash
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(
                        dream_revision_id, projection_kind, projector_version
                    ) DO NOTHING
                    """,
                    (
                        owner_user_id,
                        source.dream_revision_id,
                        source.memory_id,
                        kind.value,
                        _PROJECTOR_VERSION,
                        source.content_hash,
                    ),
                )
                if outbox_cursor.rowcount == 1:
                    inserted += 1
                id_cursor = await conn.execute(
                    """
                    SELECT id FROM memory_projection_outbox
                     WHERE dream_revision_id=? AND projection_kind=?
                       AND projector_version=?
                    """,
                    (source.dream_revision_id, kind.value, _PROJECTOR_VERSION),
                )
                outbox_row = await id_cursor.fetchone()
                if outbox_row is None:  # pragma: no cover - INSERT/UNIQUE invariant
                    raise RuntimeError("projection outbox row disappeared")
                outbox_id = int(outbox_row["id"])
                for evidence in trusted_evidence:
                    await conn.execute(
                        """
                        INSERT INTO memory_projection_evidence(outbox_id, evidence_id)
                        VALUES(?,?)
                        ON CONFLICT(outbox_id, evidence_id) DO NOTHING
                        """,
                        (outbox_id, evidence.id),
                    )
        return inserted

    async def claim(
        self,
        *,
        expected_owner_user_id: int,
        lease_owner: str,
        now: datetime,
        lease_seconds: int = 180,
    ) -> ProjectionJob | None:
        owner = int(expected_owner_user_id)
        if owner <= 0:
            raise ValueError("expected_owner_user_id must be positive")
        holder = _validate_lease_owner(lease_owner)
        now_db = _db_time(now)
        lease_until = _db_time(now + timedelta(seconds=max(30, min(lease_seconds, 900))))
        due_ready = False
        expired_ready = False
        async with get_connection() as conn:
            due_cursor = await conn.execute(
                """
                SELECT 1
                  FROM memory_projection_outbox
                 WHERE owner_user_id=?
                   AND status IN ('pending', 'retry')
                   AND due_at <= ?
                 ORDER BY due_at, id
                 LIMIT 1
                """,
                (owner, now_db),
            )
            due_ready = await due_cursor.fetchone() is not None
            expired_cursor = await conn.execute(
                """
                SELECT 1
                  FROM memory_projection_outbox
                 WHERE owner_user_id=? AND status='leased'
                   AND lease_until < ?
                 ORDER BY lease_until, id
                 LIMIT 1
                """,
                (owner, now_db),
            )
            expired_ready = await expired_cursor.fetchone() is not None
        if not due_ready and not expired_ready:
            return None

        async with write_transaction() as conn:
            if expired_ready:
                await _recover_expired_leases(conn, owner, now_db)
            cursor = await conn.execute(
                """
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
                (owner, now_db, _MAX_CLAIM_SCAN),
            )
            rows = await cursor.fetchall()
            for row in rows:
                source = await _source_from_outbox_row(conn, row)
                decision = self._policy.decide(source)
                if not decision.eligible or source.content_hash != str(row["content_hash"]):
                    reason = (
                        decision.reason
                        if not decision.eligible
                        else "content_changed"
                    )
                    await _cancel(conn, int(row["id"]), reason, now_db)
                    continue
                update = await conn.execute(
                    """
                    UPDATE memory_projection_outbox
                       SET status='leased', lease_owner=?, lease_until=?,
                           attempts=attempts+1, claimed_at=?,
                           updated_at=?, last_error_code=NULL
                     WHERE id=? AND status IN ('pending', 'retry') AND due_at <= ?
                     RETURNING attempts, max_attempts
                    """,
                    (
                        holder,
                        lease_until,
                        now_db,
                        now_db,
                        int(row["id"]),
                        now_db,
                    ),
                )
                claimed = await update.fetchone()
                if claimed is None:
                    continue
                return ProjectionJob(
                    id=int(row["id"]),
                    kind=ProjectionKind(str(row["projection_kind"])),
                    source=source,
                    attempts=int(claimed["attempts"]),
                    max_attempts=int(claimed["max_attempts"]),
                    lease_owner=holder,
                )
        return None

    async def complete(
        self,
        job: ProjectionJob,
        payload: ProjectionPayload,
        *,
        now: datetime,
    ) -> str:
        now_db = _db_time(now)
        async with write_transaction() as conn:
            row = await _leased_row(conn, job, now_db)
            source = await _source_from_outbox_row(conn, row)
            decision = self._policy.decide(source)
            if (
                not decision.eligible
                or source.content_hash != job.source.content_hash
                or source.content_hash != str(row["content_hash"])
            ):
                await _cancel(
                    conn,
                    job.id,
                    decision.reason if not decision.eligible else "content_changed",
                    now_db,
                )
                return "cancelled"

            if job.kind is ProjectionKind.GRAPH:
                if not isinstance(payload, GraphProjection):
                    raise ProjectionPayloadError("graph job requires GraphProjection")
                from app.knowledge_graph import (  # noqa: PLC0415
                    store_projection_triples_in_transaction,
                )

                units = await store_projection_triples_in_transaction(
                    conn,
                    user_id=source.owner_user_id,
                    dream_revision_id=source.dream_revision_id,
                    triples=[
                        {
                            "subject": triple.subject,
                            "relation": triple.relation,
                            "object": triple.object,
                        }
                        for triple in payload.triples
                    ],
                )
            else:
                if not isinstance(payload, EmbeddingProjection):
                    raise ProjectionPayloadError(
                        "embedding job requires EmbeddingProjection"
                    )
                units = await _store_embedding(conn, source, payload, now_db)

            update = await conn.execute(
                """
                UPDATE memory_projection_outbox
                   SET status='done', result_units=?, completed_at=?,
                       lease_owner=NULL, lease_until=NULL, updated_at=?
                 WHERE id=? AND status='leased' AND lease_owner=?
                """,
                (units, now_db, now_db, job.id, job.lease_owner),
            )
            if update.rowcount != 1:
                raise ProjectionLeaseError("projection lease was lost before completion")
            await _capability_success(conn, job.kind.value, now_db)
        return "done"

    async def fail(
        self,
        job: ProjectionJob,
        *,
        error_code: str,
        capability_status: str,
        now: datetime,
    ) -> str:
        if capability_status not in {"degraded", "unavailable"}:
            raise ValueError("invalid projection capability status")
        now_db = _db_time(now)
        clean_code = _clean_error_code(error_code)
        async with write_transaction() as conn:
            row = await _leased_row(conn, job, now_db, allow_expired=True)
            attempts = int(row["attempts"])
            max_attempts = int(row["max_attempts"])
            terminal = attempts >= max_attempts
            status = "dead" if terminal else "retry"
            delay_seconds = min(3600, 30 * (2 ** max(0, attempts - 1)))
            due_at = _db_time(now + timedelta(seconds=delay_seconds))
            await conn.execute(
                """
                UPDATE memory_projection_outbox
                   SET status=?, due_at=?, lease_owner=NULL, lease_until=NULL,
                       last_error_code=?, completed_at=CASE
                           WHEN ?='dead' THEN ? ELSE NULL END,
                       updated_at=?
                 WHERE id=? AND status='leased' AND lease_owner=?
                """,
                (
                    status,
                    due_at,
                    clean_code,
                    status,
                    now_db,
                    now_db,
                    job.id,
                    job.lease_owner,
                ),
            )
            await _capability_failure(
                conn,
                job.kind.value,
                capability_status,
                clean_code,
                now_db,
            )
        return status

    async def health_status(self) -> dict[str, Any]:
        async with get_connection() as conn:
            count_cursor = await conn.execute(
                """
                SELECT status, COUNT(*) AS n
                  FROM memory_projection_outbox
                 GROUP BY status
                """
            )
            counts = {
                str(row["status"]): int(row["n"])
                for row in await count_cursor.fetchall()
            }
            oldest_cursor = await conn.execute(
                """
                SELECT MIN(created_at) AS oldest
                  FROM memory_projection_outbox
                 WHERE status IN ('pending', 'retry', 'leased')
                """
            )
            oldest = await oldest_cursor.fetchone()
            capability_cursor = await conn.execute(
                """
                SELECT name, status, detail_code, successes, failures, checked_at
                  FROM memory_projection_capability
                 ORDER BY name
                """
            )
            capabilities = {
                str(row["name"]): {
                    "status": str(row["status"]),
                    "detail_code": (
                        str(row["detail_code"]) if row["detail_code"] else None
                    ),
                    "successes": int(row["successes"]),
                    "failures": int(row["failures"]),
                    "checked_at": str(row["checked_at"]),
                }
                for row in await capability_cursor.fetchall()
            }
            heartbeat_cursor = await conn.execute(
                """
                SELECT last_run_at, last_status, ticks
                  FROM worker_heartbeat
                 WHERE name=?
                """,
                (_HEARTBEAT_NAME,),
            )
            heartbeat = await heartbeat_cursor.fetchone()
        return {
            "counts": counts,
            "oldest_active_at": (
                str(oldest["oldest"])
                if oldest is not None and oldest["oldest"] is not None
                else None
            ),
            "capabilities": capabilities,
            "heartbeat": (
                {
                    "last_run_at": str(heartbeat["last_run_at"]),
                    "last_status": (
                        str(heartbeat["last_status"])
                        if heartbeat["last_status"] is not None
                        else None
                    ),
                    "ticks": int(heartbeat["ticks"]),
                }
                if heartbeat is not None
                else None
            ),
        }


async def _source_from_revision_row(
    conn: Any,
    *,
    owner_user_id: int,
    row: Any,
) -> ProjectionSource:
    evidence = await _evidence_for_candidate(conn, int(row["candidate_id"]))
    text = " ".join(str(row["text"] or "").split())
    return ProjectionSource(
        owner_user_id=owner_user_id,
        dream_revision_id=int(row["revision_id"]),
        memory_id=int(row["memory_id"]),
        text=text,
        content_hash=_content_hash(text),
        revision_action=str(row["revision_action"]),
        candidate_status=str(row["candidate_status"]),
        memory_pinned=bool(row["pinned"]),
        memory_active=row["valid_until"] is None,
        evidence=evidence,
    )


async def _source_from_outbox_row(conn: Any, row: Any) -> ProjectionSource:
    evidence = await _evidence_for_candidate(conn, int(row["candidate_id"]))
    text = " ".join(str(row["text"] or "").split())
    return ProjectionSource(
        owner_user_id=int(row["owner_user_id"]),
        dream_revision_id=int(row["dream_revision_id"]),
        memory_id=int(row["memory_id"]),
        text=text,
        content_hash=_content_hash(text),
        revision_action=str(row["revision_action"]),
        candidate_status=str(row["candidate_status"]),
        memory_pinned=bool(row["pinned"]),
        memory_active=row["valid_until"] is None,
        evidence=evidence,
    )


async def _evidence_for_candidate(
    conn: Any,
    candidate_id: int,
) -> tuple[ProjectionEvidence, ...]:
    cursor = await conn.execute(
        """
        SELECT id, source_kind, owner_attributed, content_hash, excerpt
          FROM dream_evidence
         WHERE candidate_id=?
         ORDER BY id
        """,
        (candidate_id,),
    )
    return tuple(
        ProjectionEvidence(
            id=int(row["id"]),
            source_kind=str(row["source_kind"]),
            owner_attributed=bool(row["owner_attributed"]),
            content_hash=str(row["content_hash"]),
            excerpt=str(row["excerpt"]) if row["excerpt"] is not None else None,
        )
        for row in await cursor.fetchall()
    )


async def _leased_row(
    conn: Any,
    job: ProjectionJob,
    now_db: str,
    *,
    allow_expired: bool = False,
) -> Any:
    if allow_expired:
        cursor = await conn.execute(
            """
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
             WHERE o.id=? AND o.status='leased' AND o.lease_owner=?
            """,
            (job.id, job.lease_owner),
        )
    else:
        cursor = await conn.execute(
            """
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
             WHERE o.id=? AND o.status='leased' AND o.lease_owner=?
               AND o.lease_until >= ?
            """,
            (job.id, job.lease_owner, now_db),
        )
    row = await cursor.fetchone()
    if row is None:
        raise ProjectionLeaseError("projection job lease was lost")
    return row


async def _recover_expired_leases(
    conn: Any,
    owner_user_id: int,
    now_db: str,
) -> None:
    await conn.execute(
        """
        UPDATE memory_projection_outbox
           SET status=CASE WHEN attempts >= max_attempts THEN 'dead' ELSE 'retry' END,
               due_at=?,
               last_error_code='lease_expired',
               completed_at=CASE WHEN attempts >= max_attempts THEN ? ELSE NULL END,
               lease_owner=NULL, lease_until=NULL, updated_at=?
         WHERE owner_user_id=? AND status='leased' AND lease_until < ?
        """,
        (now_db, now_db, now_db, owner_user_id, now_db),
    )


async def _cancel(conn: Any, outbox_id: int, reason: str, now_db: str) -> None:
    await conn.execute(
        """
        UPDATE memory_projection_outbox
           SET status='cancelled', last_error_code=?, completed_at=?,
               lease_owner=NULL, lease_until=NULL, updated_at=?
         WHERE id=? AND status NOT IN ('done', 'dead', 'cancelled')
        """,
        (_clean_error_code(reason), now_db, now_db, outbox_id),
    )


async def _store_embedding(
    conn: Any,
    source: ProjectionSource,
    payload: EmbeddingProjection,
    now_db: str,
) -> int:
    vector = tuple(float(item) for item in payload.vector)
    if not 1 <= len(vector) <= _MAX_EMBEDDING_DIMENSIONS or not all(
        map(math.isfinite, vector)
    ):
        raise ProjectionPayloadError("embedding dimension must be in 1..4096")
    blob = struct.pack(f"<{len(vector)}f", *vector)
    await conn.execute(
        """
        INSERT INTO memory_revision_embedding(
            dream_revision_id, owner_user_id, memory_id, content_hash,
            model_name, dimensions, embedding, created_at, updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(dream_revision_id) DO UPDATE SET
            owner_user_id=excluded.owner_user_id,
            memory_id=excluded.memory_id,
            content_hash=excluded.content_hash,
            model_name=excluded.model_name,
            dimensions=excluded.dimensions,
            embedding=excluded.embedding,
            updated_at=excluded.updated_at
        """,
        (
            source.dream_revision_id,
            source.owner_user_id,
            source.memory_id,
            source.content_hash,
            payload.model_name[:120] or "configured",
            len(vector),
            blob,
            now_db,
            now_db,
        ),
    )
    return len(vector)


async def _capability_success(conn: Any, name: str, now_db: str) -> None:
    await conn.execute(
        """
        INSERT INTO memory_projection_capability(
            name, status, detail_code, successes, failures, checked_at
        ) VALUES(?, 'ready', NULL, 1, 0, ?)
        ON CONFLICT(name) DO UPDATE SET
            status='ready', detail_code=NULL,
            successes=memory_projection_capability.successes+1,
            checked_at=excluded.checked_at
        """,
        (name, now_db),
    )


async def _capability_failure(
    conn: Any,
    name: str,
    status: str,
    detail_code: str,
    now_db: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO memory_projection_capability(
            name, status, detail_code, successes, failures, checked_at
        ) VALUES(?,?,?,0,1,?)
        ON CONFLICT(name) DO UPDATE SET
            status=excluded.status,
            detail_code=excluded.detail_code,
            failures=memory_projection_capability.failures+1,
            checked_at=excluded.checked_at
        """,
        (name, status, detail_code, now_db),
    )


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _validate_lease_owner(value: str) -> str:
    clean = str(value or "").strip()
    if not _LEASE_OWNER_RE.fullmatch(clean):
        raise ValueError("invalid projection lease owner")
    return clean


def _clean_error_code(value: str) -> str:
    clean = "".join(
        character
        for character in str(value or "")
        if character.isalnum() or character in "._-"
    )
    return (clean or "projection_error")[:80]


def _db_time(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


__all__ = [
    "ProjectionLeaseError",
    "ProjectionPayloadError",
    "SqliteProjectionOutbox",
]
