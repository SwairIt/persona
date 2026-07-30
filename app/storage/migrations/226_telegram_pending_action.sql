-- Task 2 (2026-07-30 full-tool-access plan): one-shot parking table for
-- execution-class Telegram actions (run_shell, run_mac, install_mcp,
-- install_skill, write_file, delete_path). A row here is claimed exactly
-- once by a conditional UPDATE (see PendingActionStore.claim) -- the
-- inline confirm button reads arguments back from this table, never from
-- the callback payload.
CREATE TABLE IF NOT EXISTS telegram_pending_action (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_user_id INTEGER NOT NULL,
    telegram_chat_id INTEGER NOT NULL,
    tool_name       TEXT NOT NULL,
    args_json       TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT NOT NULL,
    consumed_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_telegram_pending_action_live
    ON telegram_pending_action(persona_user_id, consumed_at, expires_at);
