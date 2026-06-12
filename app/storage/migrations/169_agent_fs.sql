-- T29 (2026-06-12) — remote filesystem RPC over the Mac agent.
-- The AI's file tools (read/list/write) run ON the Mac via the agent
-- instead of the server workspace, so nothing the AI creates is stored on
-- the server. The agent enforces an allowlist of directories the user
-- configured (and creates them if missing).
CREATE TABLE IF NOT EXISTS agent_fs_command (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id    INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    op           TEXT    NOT NULL,             -- read | list | write
    path         TEXT    NOT NULL,
    content      TEXT,                         -- for write
    status       TEXT    NOT NULL DEFAULT 'pending',  -- pending | done | error
    result       TEXT,                         -- file content / listing / message
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_fs_cmd_pending ON agent_fs_command(device_id, status);

-- allowlist roots (newline-separated; ~ expands to the Mac home) + master
-- switch. Default: ~/Projects. User edits both on /settings/mac-fs.
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('mac_fs_roots', '~/Projects');
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('mac_fs_enabled', '0');
