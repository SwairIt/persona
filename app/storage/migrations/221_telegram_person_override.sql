CREATE TABLE IF NOT EXISTS telegram_person_override (
    persona_user_id       INTEGER NOT NULL,
    telegram_user_id      INTEGER NOT NULL,
    display_name_override TEXT,
    note                  TEXT,
    ignored               INTEGER NOT NULL DEFAULT 0 CHECK (ignored IN (0, 1)),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (persona_user_id, telegram_user_id),
    FOREIGN KEY (persona_user_id, telegram_user_id)
        REFERENCES telegram_person(persona_user_id, telegram_user_id)
        ON DELETE CASCADE
);
