-- Evidence-linked graph/embedding projection after durable memory revisions.
--
-- The dream completion transaction inserts these intents.  A separate leased
-- worker performs model/network I/O without holding a SQLite transaction, then
-- atomically stores the projection and terminal outbox state.
CREATE TABLE IF NOT EXISTS memory_projection_outbox (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id       INTEGER NOT NULL
                        REFERENCES users(id) ON DELETE CASCADE,
    dream_revision_id   INTEGER NOT NULL
                        REFERENCES dream_revision(id) ON DELETE CASCADE,
    memory_id           INTEGER NOT NULL
                        REFERENCES user_memory(id) ON DELETE CASCADE,
    projection_kind     TEXT NOT NULL
                        CHECK (projection_kind IN ('graph', 'embedding')),
    projector_version   INTEGER NOT NULL DEFAULT 1,
    content_hash        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending', 'leased', 'retry', 'done', 'dead', 'cancelled'
                        )),
    attempts            INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 5,
    due_at              TEXT NOT NULL DEFAULT (datetime('now')),
    lease_owner         TEXT,
    lease_until         TEXT,
    last_error_code     TEXT,
    result_units        INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    claimed_at          TEXT,
    completed_at        TEXT,
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(dream_revision_id, projection_kind, projector_version)
);

CREATE TABLE IF NOT EXISTS memory_projection_evidence (
    outbox_id           INTEGER NOT NULL
                        REFERENCES memory_projection_outbox(id) ON DELETE CASCADE,
    evidence_id         INTEGER NOT NULL
                        REFERENCES dream_evidence(id) ON DELETE CASCADE,
    PRIMARY KEY(outbox_id, evidence_id)
);

-- Embeddings for curated memory revisions are not chat-message embeddings.
-- Keep a dedicated source-of-truth table that works even when sqlite-vec is
-- unavailable; an optional accelerator can be added later without data loss.
CREATE TABLE IF NOT EXISTS memory_revision_embedding (
    dream_revision_id   INTEGER PRIMARY KEY
                        REFERENCES dream_revision(id) ON DELETE CASCADE,
    owner_user_id       INTEGER NOT NULL
                        REFERENCES users(id) ON DELETE CASCADE,
    memory_id           INTEGER NOT NULL
                        REFERENCES user_memory(id) ON DELETE CASCADE,
    content_hash        TEXT NOT NULL,
    model_name          TEXT NOT NULL,
    dimensions          INTEGER NOT NULL,
    embedding           BLOB NOT NULL,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One revision/triple projection record prevents a crash after graph storage
-- from incrementing the same edge twice on retry.
CREATE TABLE IF NOT EXISTS graph_revision_projection (
    dream_revision_id   INTEGER NOT NULL
                        REFERENCES dream_revision(id) ON DELETE CASCADE,
    triple_hash         TEXT NOT NULL,
    kg_edge_id          INTEGER NOT NULL
                        REFERENCES kg_edge(id) ON DELETE CASCADE,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(dream_revision_id, triple_hash)
);

CREATE TABLE IF NOT EXISTS memory_projection_capability (
    name                TEXT PRIMARY KEY
                        CHECK (name IN ('graph', 'embedding')),
    status              TEXT NOT NULL
                        CHECK (status IN ('unknown', 'ready', 'degraded', 'unavailable')),
    detail_code         TEXT,
    successes           INTEGER NOT NULL DEFAULT 0,
    failures            INTEGER NOT NULL DEFAULT 0,
    checked_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO memory_projection_capability(name, status)
VALUES('graph', 'unknown')
ON CONFLICT(name) DO NOTHING;
INSERT INTO memory_projection_capability(name, status)
VALUES('embedding', 'unknown')
ON CONFLICT(name) DO NOTHING;

CREATE INDEX IF NOT EXISTS idx_memory_projection_claim
    ON memory_projection_outbox(status, due_at, lease_until, id);
CREATE INDEX IF NOT EXISTS idx_memory_projection_owner
    ON memory_projection_outbox(owner_user_id, status, id);
CREATE INDEX IF NOT EXISTS idx_memory_projection_revision
    ON memory_projection_outbox(dream_revision_id, id);
CREATE INDEX IF NOT EXISTS idx_memory_projection_evidence
    ON memory_projection_evidence(evidence_id, outbox_id);

-- Existing graph tables predate users FKs. Privacy erasure must not leave graph
-- entities/edges after the owner row is removed.
CREATE TRIGGER IF NOT EXISTS projection_user_privacy_purge_before
BEFORE DELETE ON users
BEGIN
    DELETE FROM kg_edge WHERE user_id=OLD.id;
    DELETE FROM kg_entity WHERE user_id=OLD.id;
END;
