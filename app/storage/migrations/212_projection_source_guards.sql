-- Append-only correction for the already-applied migration 210.
--
-- Cross-table owner/linkage invariants cannot be represented by a plain
-- SQLite foreign key. Reject malformed/manual rows at the storage boundary.
CREATE INDEX IF NOT EXISTS idx_memory_projection_owner_due
    ON memory_projection_outbox(owner_user_id, due_at, id, status);

CREATE INDEX IF NOT EXISTS idx_memory_projection_owner_lease
    ON memory_projection_outbox(owner_user_id, status, lease_until, id);

CREATE TRIGGER IF NOT EXISTS memory_projection_outbox_owner_guard_insert
BEFORE INSERT ON memory_projection_outbox
WHEN NOT EXISTS (
    SELECT 1
      FROM dream_revision r
      JOIN dream_run run ON run.id=r.run_id
      JOIN dream_candidate c
        ON c.id=r.candidate_id AND c.run_id=r.run_id
      JOIN user_memory m ON m.id=r.memory_id
     WHERE r.id=NEW.dream_revision_id
       AND r.memory_id=NEW.memory_id
       AND run.user_id=NEW.owner_user_id
       AND m.user_id=NEW.owner_user_id
)
BEGIN
    SELECT RAISE(ABORT, 'projection owner/source mismatch');
END;

CREATE TRIGGER IF NOT EXISTS memory_projection_outbox_owner_guard_update
BEFORE UPDATE OF owner_user_id, dream_revision_id, memory_id
ON memory_projection_outbox
WHEN NOT EXISTS (
    SELECT 1
      FROM dream_revision r
      JOIN dream_run run ON run.id=r.run_id
      JOIN dream_candidate c
        ON c.id=r.candidate_id AND c.run_id=r.run_id
      JOIN user_memory m ON m.id=r.memory_id
     WHERE r.id=NEW.dream_revision_id
       AND r.memory_id=NEW.memory_id
       AND run.user_id=NEW.owner_user_id
       AND m.user_id=NEW.owner_user_id
)
BEGIN
    SELECT RAISE(ABORT, 'projection owner/source mismatch');
END;

CREATE TRIGGER IF NOT EXISTS memory_projection_evidence_guard
BEFORE INSERT ON memory_projection_evidence
WHEN NOT EXISTS (
    SELECT 1
      FROM memory_projection_outbox o
      JOIN dream_revision r ON r.id=o.dream_revision_id
      JOIN dream_candidate c
        ON c.id=r.candidate_id AND c.run_id=r.run_id
      JOIN dream_evidence e
        ON e.id=NEW.evidence_id AND e.candidate_id=c.id
     WHERE o.id=NEW.outbox_id
)
BEGIN
    SELECT RAISE(ABORT, 'projection evidence/source mismatch');
END;

CREATE TRIGGER IF NOT EXISTS memory_projection_evidence_guard_update
BEFORE UPDATE OF outbox_id, evidence_id
ON memory_projection_evidence
WHEN NOT EXISTS (
    SELECT 1
      FROM memory_projection_outbox o
      JOIN dream_revision r ON r.id=o.dream_revision_id
      JOIN dream_candidate c
        ON c.id=r.candidate_id AND c.run_id=r.run_id
      JOIN dream_evidence e
        ON e.id=NEW.evidence_id AND e.candidate_id=c.id
     WHERE o.id=NEW.outbox_id
)
BEGIN
    SELECT RAISE(ABORT, 'projection evidence/source mismatch');
END;
