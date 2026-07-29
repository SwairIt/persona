CREATE TABLE IF NOT EXISTS telegram_pinned_chat (
    persona_user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    telegram_chat_id            INTEGER NOT NULL,
    title                       TEXT NOT NULL DEFAULT '',
    active                      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    last_imported_message_id    INTEGER NOT NULL DEFAULT 0,
    last_analyzed_row_id        INTEGER NOT NULL DEFAULT 0,
    discovered_at               TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (persona_user_id, telegram_chat_id)
);

CREATE TABLE IF NOT EXISTS telegram_pinned_message (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    telegram_chat_id     INTEGER NOT NULL,
    telegram_message_id  INTEGER NOT NULL,
    telegram_sender_id   INTEGER,
    sender_label         TEXT NOT NULL DEFAULT '',
    text                 TEXT NOT NULL,
    sent_at              TEXT,
    imported_at          TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (persona_user_id, telegram_chat_id, telegram_message_id),
    FOREIGN KEY (persona_user_id, telegram_chat_id)
        REFERENCES telegram_pinned_chat(persona_user_id, telegram_chat_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_telegram_pinned_message_analysis
    ON telegram_pinned_message(persona_user_id, telegram_chat_id, id);
