-- Research on request (owner mandate 2026-07-30/31): a chain can now be
-- seeded because someone in a chat asked Persona to look something up
-- ("персик, посмотри Лабиринт Фавна"), not only from the self-directed
-- rotation in app/thinking/settings.py. That needs a new seed_kind value,
-- but SQLite cannot ALTER a CHECK constraint, so persona_thought is rebuilt:
-- rename to a legacy table, create the new one with 'research' added to the
-- seed_kind CHECK, copy every existing row byte-for-byte, drop the legacy
-- table, recreate the index that lived on it.
--
-- persona_thought_chain only needs a new column (which chat to answer back
-- into) -- that has no CHECK constraint, so a plain ADD COLUMN is enough,
-- no rebuild required there.

ALTER TABLE persona_thought RENAME TO persona_thought_legacy_227;

CREATE TABLE persona_thought (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_user_id   INTEGER NOT NULL,
    chain_id          INTEGER NOT NULL,
    step_no           INTEGER NOT NULL,
    kind              TEXT NOT NULL CHECK (kind IN ('seed', 'step', 'conclusion')),
    seed_kind         TEXT NOT NULL CHECK (
                          seed_kind IN (
                              'know_you', 'unfinished', 'self_check', 'alive',
                              'research'
                          )
                      ),
    text              TEXT NOT NULL,
    source_scope      TEXT NOT NULL,
    source_session_id INTEGER,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    certainty         TEXT NOT NULL DEFAULT 'guess'
                          CHECK (certainty IN ('observation', 'guess')),
    confirmed_at      TEXT,
    UNIQUE (chain_id, step_no)
);

INSERT INTO persona_thought(
    id, persona_user_id, chain_id, step_no, kind, seed_kind, text,
    source_scope, source_session_id, created_at, certainty, confirmed_at
)
SELECT
    id, persona_user_id, chain_id, step_no, kind, seed_kind, text,
    source_scope, source_session_id, created_at, certainty, confirmed_at
FROM persona_thought_legacy_227;

DROP TABLE persona_thought_legacy_227;

CREATE INDEX IF NOT EXISTS idx_persona_thought_recent
    ON persona_thought(persona_user_id, created_at DESC);

-- Which chat (Telegram chat id) this chain must answer back into, when it
-- was seeded from a research request. NULL for every non-research chain.
ALTER TABLE persona_thought_chain ADD COLUMN source_chat_id INTEGER;
