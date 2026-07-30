-- Task 1 (2026-07-30 plan): per-chat owner preferences for Telegram access.
-- The settings page (Task 3) writes here; `mode="reply"` write-through keeps
-- the existing `telegram_allowed_chat_ids` kv (the transport's source of
-- truth) in sync -- see TelegramRepository.set_chat_pref.
CREATE TABLE IF NOT EXISTS telegram_chat_pref (
    telegram_chat_id INTEGER PRIMARY KEY,
    title            TEXT NOT NULL DEFAULT '',
    mode             TEXT NOT NULL DEFAULT 'read'
                         CHECK (mode IN ('reply', 'read', 'ignore')),
    ingest           INTEGER NOT NULL DEFAULT 0 CHECK (ingest IN (0, 1)),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
