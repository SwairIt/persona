-- Per-user settings (MVP: per-user LLM provider/key/model + UI prefs).
-- Mirrors kv_settings but scoped by user_id. Owner keeps using global
-- kv_settings -- nothing here migrates or shadows the existing rows; a
-- registered non-owner user simply gets its own namespace.
--
-- ON DELETE CASCADE matches auth_session (migration 151): deleting the user
-- row must not leave an orphan provider key behind. Cascade only fires on
-- connections with ``PRAGMA foreign_keys = ON`` -- app/storage/db.py
-- ``_configure_connection`` sets it for every read and write connection.
CREATE TABLE IF NOT EXISTS user_settings (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key        TEXT    NOT NULL,
    value      TEXT    NOT NULL,
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, key)
);

-- Who spent the tokens. NULL = the owner / any background job running on the
-- global kv_settings credentials, which is every row written before this
-- migration. Nullable on purpose: llm_usage is an append-only audit table and
-- backfilling an owner id here would invent data.
ALTER TABLE llm_usage ADD COLUMN user_id INTEGER;
