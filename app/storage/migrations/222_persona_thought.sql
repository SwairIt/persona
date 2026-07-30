CREATE TABLE IF NOT EXISTS persona_thought (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_user_id   INTEGER NOT NULL,
    chain_id          INTEGER NOT NULL,
    step_no           INTEGER NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('seed', 'step', 'conclusion')),
    seed_kind         TEXT NOT NULL CHECK (
                          seed_kind IN ('know_you', 'unfinished', 'self_check', 'alive')
                      ),
    text              TEXT NOT NULL,
    source_scope      TEXT NOT NULL,
    source_session_id INTEGER,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (chain_id, step_no)
);

CREATE TABLE IF NOT EXISTS persona_thought_chain (
    chain_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_user_id   INTEGER NOT NULL,
    seed_kind         TEXT NOT NULL,
    source_scope      TEXT NOT NULL,
    source_session_id INTEGER,
    status            TEXT NOT NULL DEFAULT 'open'
                          CHECK (status IN ('open', 'closed')),
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_persona_thought_chain_open
    ON persona_thought_chain(persona_user_id, status, created_at);

CREATE INDEX IF NOT EXISTS idx_persona_thought_recent
    ON persona_thought(persona_user_id, created_at DESC);
