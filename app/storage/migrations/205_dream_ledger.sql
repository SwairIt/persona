-- Durable, auditable nightly-memory pipeline.
--
-- The generative phase may only write proposals and evidence.  A separate
-- deterministic policy applies an approved proposal to user_memory in a short
-- transaction and records the exact before/after revision.  Audit, evidence,
-- and revision rows are append-only by database invariant.
CREATE TABLE IF NOT EXISTS dream_run (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL
                        REFERENCES users(id) ON DELETE CASCADE,
    idempotency_key     TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending', 'running', 'retry', 'completed', 'cancelled'
                        )),
    worker_id           TEXT,
    lease_until         TEXT,
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    input_cursor        INTEGER NOT NULL DEFAULT 0,
    safe_cursor         INTEGER NOT NULL DEFAULT 0,
    config_json         TEXT NOT NULL DEFAULT '{}',
    candidates_count    INTEGER NOT NULL DEFAULT 0,
    applied_count       INTEGER NOT NULL DEFAULT 0,
    rejected_count      INTEGER NOT NULL DEFAULT 0,
    error               TEXT,
    retry_at            TEXT,
    started_at          TEXT,
    completed_at        TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS dream_candidate (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL
                        REFERENCES dream_run(id) ON DELETE CASCADE,
    candidate_key       TEXT NOT NULL,
    text                TEXT NOT NULL,
    kind                TEXT NOT NULL,
    proposed_action     TEXT NOT NULL DEFAULT 'add'
                        CHECK (proposed_action IN ('add', 'update', 'noop', 'delete')),
    target_memory_id    INTEGER,
    score               REAL NOT NULL,
    observed_count      INTEGER NOT NULL DEFAULT 1,
    source_count        INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'proposed'
                        CHECK (status IN (
                            'proposed', 'applied', 'rejected', 'noop', 'failed'
                        )),
    policy_reason       TEXT,
    result_memory_id    INTEGER,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at          TEXT,
    UNIQUE(run_id, candidate_key)
);

CREATE TABLE IF NOT EXISTS dream_evidence (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id        INTEGER NOT NULL
                        REFERENCES dream_candidate(id) ON DELETE CASCADE,
    evidence_key        TEXT NOT NULL,
    source_kind         TEXT NOT NULL
                        CHECK (source_kind IN (
                            'owner_chat', 'telegram_group', 'screen', 'audio', 'other'
                        )),
    source_ref          TEXT NOT NULL,
    source_message_id   INTEGER,
    owner_attributed    INTEGER NOT NULL DEFAULT 0
                        CHECK (owner_attributed IN (0, 1)),
    content_hash        TEXT NOT NULL,
    excerpt             TEXT,
    observed_at         TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(candidate_id, evidence_key)
);

CREATE TABLE IF NOT EXISTS dream_revision (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL
                        REFERENCES dream_run(id) ON DELETE RESTRICT,
    candidate_id        INTEGER NOT NULL
                        REFERENCES dream_candidate(id) ON DELETE RESTRICT,
    action              TEXT NOT NULL
                        CHECK (action IN ('add', 'update', 'noop', 'reject', 'rollback')),
    memory_id           INTEGER,
    prior_json          TEXT,
    result_json         TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dream_audit (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              INTEGER NOT NULL
                        REFERENCES dream_run(id) ON DELETE RESTRICT,
    candidate_id        INTEGER
                        REFERENCES dream_candidate(id) ON DELETE RESTRICT,
    event               TEXT NOT NULL,
    detail_json         TEXT NOT NULL DEFAULT '{}',
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Link the legacy human-facing report to exactly one durable run.  Completion,
-- report creation, and cursor movement are committed by one repository
-- transaction.
ALTER TABLE dream_report ADD COLUMN run_id INTEGER
    REFERENCES dream_run(id) ON DELETE CASCADE;

CREATE UNIQUE INDEX IF NOT EXISTS idx_dream_report_run
    ON dream_report(run_id) WHERE run_id IS NOT NULL;

-- Append-only protects normal operation, but privacy erasure must hard-delete
-- all subject data.  The guard is populated only by the users DELETE trigger,
-- remains inside that same SQLite transaction, and is removed afterwards.
CREATE TABLE IF NOT EXISTS dream_privacy_purge_guard (
    user_id             INTEGER PRIMARY KEY,
    requested_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_dream_run_claim
    ON dream_run(status, retry_at, lease_until, id);
CREATE INDEX IF NOT EXISTS idx_dream_candidate_run
    ON dream_candidate(run_id, status, id);
CREATE INDEX IF NOT EXISTS idx_dream_evidence_candidate
    ON dream_evidence(candidate_id, id);
CREATE INDEX IF NOT EXISTS idx_dream_revision_run
    ON dream_revision(run_id, id);
CREATE INDEX IF NOT EXISTS idx_dream_audit_run
    ON dream_audit(run_id, id);

CREATE TRIGGER IF NOT EXISTS dream_evidence_no_update
BEFORE UPDATE ON dream_evidence
BEGIN
    SELECT RAISE(ABORT, 'dream_evidence is append-only');
END;

CREATE TRIGGER IF NOT EXISTS dream_evidence_no_delete
BEFORE DELETE ON dream_evidence
WHEN NOT EXISTS (
    SELECT 1
      FROM dream_privacy_purge_guard g
      JOIN dream_run r ON r.user_id=g.user_id
      JOIN dream_candidate c ON c.run_id=r.id
     WHERE c.id=OLD.candidate_id
)
BEGIN
    SELECT RAISE(ABORT, 'dream_evidence is append-only');
END;

CREATE TRIGGER IF NOT EXISTS dream_revision_no_update
BEFORE UPDATE ON dream_revision
BEGIN
    SELECT RAISE(ABORT, 'dream_revision is append-only');
END;

CREATE TRIGGER IF NOT EXISTS dream_revision_no_delete
BEFORE DELETE ON dream_revision
WHEN NOT EXISTS (
    SELECT 1
      FROM dream_privacy_purge_guard g
      JOIN dream_run r ON r.user_id=g.user_id
     WHERE r.id=OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'dream_revision is append-only');
END;

CREATE TRIGGER IF NOT EXISTS dream_audit_no_update
BEFORE UPDATE ON dream_audit
BEGIN
    SELECT RAISE(ABORT, 'dream_audit is append-only');
END;

CREATE TRIGGER IF NOT EXISTS dream_audit_no_delete
BEFORE DELETE ON dream_audit
WHEN NOT EXISTS (
    SELECT 1
      FROM dream_privacy_purge_guard g
      JOIN dream_run r ON r.user_id=g.user_id
     WHERE r.id=OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'dream_audit is append-only');
END;

CREATE TRIGGER IF NOT EXISTS dream_candidate_no_delete
BEFORE DELETE ON dream_candidate
WHEN NOT EXISTS (
    SELECT 1
      FROM dream_privacy_purge_guard g
      JOIN dream_run r ON r.user_id=g.user_id
     WHERE r.id=OLD.run_id
)
BEGIN
    SELECT RAISE(ABORT, 'dream_candidate is append-only');
END;

CREATE TRIGGER IF NOT EXISTS dream_run_no_delete
BEFORE DELETE ON dream_run
WHEN NOT EXISTS (
    SELECT 1
      FROM dream_privacy_purge_guard g
     WHERE g.user_id=OLD.user_id
)
BEGIN
    SELECT RAISE(ABORT, 'dream_run is append-only');
END;

-- Explicit privacy-erasure contract.  Children with RESTRICT FKs are removed
-- in dependency order while the per-user guard is active.  Any failure aborts
-- the outer DELETE and rolls the entire purge back.
CREATE TRIGGER IF NOT EXISTS dream_user_privacy_purge_before
BEFORE DELETE ON users
BEGIN
    INSERT INTO dream_privacy_purge_guard(user_id, requested_at)
    VALUES(OLD.id, datetime('now'))
    ON CONFLICT(user_id) DO UPDATE SET requested_at=datetime('now');

    DELETE FROM dream_evidence
     WHERE candidate_id IN (
         SELECT c.id
           FROM dream_candidate c
           JOIN dream_run r ON r.id=c.run_id
          WHERE r.user_id=OLD.id
     );
    DELETE FROM dream_revision
     WHERE run_id IN (SELECT id FROM dream_run WHERE user_id=OLD.id);
    DELETE FROM dream_audit
     WHERE run_id IN (SELECT id FROM dream_run WHERE user_id=OLD.id);
    DELETE FROM dream_candidate
     WHERE run_id IN (SELECT id FROM dream_run WHERE user_id=OLD.id);
    DELETE FROM dream_run WHERE user_id=OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS dream_user_privacy_purge_after
AFTER DELETE ON users
BEGIN
    DELETE FROM dream_privacy_purge_guard WHERE user_id=OLD.id;
END;
