-- Append-only hardening for the already-applied migration 212.
--
-- Bind every revision to a candidate from the same dream run and enforce the
-- evidence relationship on updates as well as inserts.
DROP TRIGGER IF EXISTS memory_projection_outbox_owner_guard_insert;
DROP TRIGGER IF EXISTS memory_projection_outbox_owner_guard_update;
DROP TRIGGER IF EXISTS memory_projection_evidence_guard;
DROP TRIGGER IF EXISTS memory_projection_evidence_guard_update;

CREATE TRIGGER memory_projection_outbox_owner_guard_insert
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

CREATE TRIGGER memory_projection_outbox_owner_guard_update
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

CREATE TRIGGER memory_projection_evidence_guard
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

CREATE TRIGGER memory_projection_evidence_guard_update
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
