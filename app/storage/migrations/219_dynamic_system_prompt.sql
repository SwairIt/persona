CREATE TABLE IF NOT EXISTS dynamic_system_prompt_config (
    persona_user_id  INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    enabled          INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    rules_json       TEXT NOT NULL DEFAULT '[]',
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dynamic_system_prompt_version (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    version_number   INTEGER NOT NULL CHECK (version_number > 0),
    mode             TEXT NOT NULL,
    prompt_text      TEXT NOT NULL,
    reason           TEXT NOT NULL,
    source_surface   TEXT NOT NULL,
    source_excerpt   TEXT NOT NULL DEFAULT '',
    is_active        INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (persona_user_id, version_number)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_dynamic_system_prompt_active
    ON dynamic_system_prompt_version(persona_user_id)
    WHERE is_active = 1;

CREATE INDEX IF NOT EXISTS idx_dynamic_system_prompt_history
    ON dynamic_system_prompt_version(
        persona_user_id, version_number DESC, created_at DESC
    );
