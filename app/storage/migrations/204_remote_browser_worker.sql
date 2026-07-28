-- Durable, outbound-only Playwright worker queue.
--
-- A browser session is owned by one Persona user/chat session and is bound to
-- one stable PC worker id.  Jobs carry only the allowlisted browser action
-- payload; no shell command or filesystem path is part of this protocol.
CREATE TABLE IF NOT EXISTS remote_browser_session (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id       INTEGER NOT NULL,
    conversation_id     INTEGER NOT NULL,
    assigned_worker_id  TEXT,
    active_job_id       INTEGER,
    state               TEXT NOT NULL DEFAULT 'open'
                        CHECK (state IN ('open', 'closed', 'error')),
    last_url            TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(owner_user_id, conversation_id)
);

CREATE TABLE IF NOT EXISTS remote_browser_job (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    browser_session_id  INTEGER NOT NULL
                        REFERENCES remote_browser_session(id) ON DELETE CASCADE,
    owner_user_id       INTEGER NOT NULL,
    conversation_id     INTEGER NOT NULL,
    correlation_id      TEXT NOT NULL,
    action              TEXT NOT NULL
                        CHECK (action IN (
                            'open', 'click', 'type', 'read',
                            'screenshot', 'close', 'ping'
                        )),
    payload              TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending', 'claimed', 'done', 'error', 'cancelled'
                        )),
    target_worker_id     TEXT,
    worker_id            TEXT,
    result               TEXT,
    error                TEXT,
    cancel_requested     INTEGER NOT NULL DEFAULT 0
                        CHECK (cancel_requested IN (0, 1)),
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    claimed_at           TEXT,
    lease_until          TEXT,
    finished_at          TEXT,
    UNIQUE(owner_user_id, correlation_id)
);

CREATE TABLE IF NOT EXISTS remote_browser_worker_presence (
    worker_id           TEXT PRIMARY KEY,
    last_seen           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_remote_browser_job_claim
    ON remote_browser_job(status, target_worker_id, id);
CREATE INDEX IF NOT EXISTS idx_remote_browser_job_session
    ON remote_browser_job(browser_session_id, status, id);
CREATE INDEX IF NOT EXISTS idx_remote_browser_session_worker
    ON remote_browser_session(assigned_worker_id, updated_at);
CREATE INDEX IF NOT EXISTS idx_remote_browser_worker_seen
    ON remote_browser_worker_presence(last_seen);

-- Existing installs used server-local Playwright by default. This release
-- moves that default to the owner's outbound-only PC worker while preserving
-- an explicit MCP/both choice.
INSERT INTO kv_settings(key, value, updated_at)
VALUES ('browser_backend', 'remote', datetime('now'))
ON CONFLICT(key) DO UPDATE SET
    value=CASE
        WHEN kv_settings.value='builtin' THEN 'remote'
        ELSE kv_settings.value
    END,
    updated_at=datetime('now');
