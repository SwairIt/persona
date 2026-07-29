CREATE TABLE IF NOT EXISTS telegram_person (
    persona_user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    telegram_user_id  INTEGER NOT NULL CHECK (telegram_user_id > 0),
    username          TEXT,
    first_name        TEXT,
    last_name         TEXT,
    display_name      TEXT NOT NULL,
    language_code     TEXT,
    is_bot            INTEGER NOT NULL DEFAULT 0 CHECK (is_bot IN (0, 1)),
    is_owner          INTEGER NOT NULL DEFAULT 0 CHECK (is_owner IN (0, 1)),
    first_seen_at     TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_chat_id      INTEGER,
    message_count     INTEGER NOT NULL DEFAULT 0 CHECK (message_count >= 0),
    PRIMARY KEY (persona_user_id, telegram_user_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_telegram_person_single_owner
    ON telegram_person(persona_user_id)
    WHERE is_owner = 1;

CREATE INDEX IF NOT EXISTS idx_telegram_person_recent
    ON telegram_person(persona_user_id, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS telegram_person_message (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_user_id            INTEGER NOT NULL,
    telegram_user_id           INTEGER NOT NULL,
    telegram_chat_id           INTEGER NOT NULL,
    telegram_message_id        INTEGER NOT NULL,
    reply_to_telegram_user_id  INTEGER,
    text                       TEXT NOT NULL,
    sent_at                    TEXT,
    observed_at                TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (persona_user_id, telegram_user_id)
        REFERENCES telegram_person(persona_user_id, telegram_user_id)
        ON DELETE CASCADE,
    UNIQUE (telegram_chat_id, telegram_message_id)
);

CREATE INDEX IF NOT EXISTS idx_telegram_person_message_subject
    ON telegram_person_message(
        persona_user_id, telegram_user_id, observed_at DESC, id DESC
    );

CREATE INDEX IF NOT EXISTS idx_telegram_person_message_chat
    ON telegram_person_message(
        persona_user_id, telegram_chat_id, observed_at DESC, id DESC
    );

CREATE TABLE IF NOT EXISTS telegram_person_fact (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_user_id    INTEGER NOT NULL,
    telegram_user_id   INTEGER NOT NULL,
    text               TEXT NOT NULL,
    normalized_hash    TEXT NOT NULL,
    kind               TEXT NOT NULL DEFAULT 'self_statement',
    source_chat_id     INTEGER,
    source_message_id  INTEGER,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now')),
    valid_until        TEXT,
    FOREIGN KEY (persona_user_id, telegram_user_id)
        REFERENCES telegram_person(persona_user_id, telegram_user_id)
        ON DELETE CASCADE,
    UNIQUE (persona_user_id, telegram_user_id, normalized_hash)
);

CREATE INDEX IF NOT EXISTS idx_telegram_person_fact_active
    ON telegram_person_fact(
        persona_user_id, telegram_user_id, valid_until, updated_at DESC
    );
