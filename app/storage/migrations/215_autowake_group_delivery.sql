-- Add privacy-scoped Telegram group delivery to the durable autowake outbox.
--
-- Existing rows remain owner-DM-only. Group rows must carry a negative,
-- allowlisted Telegram chat id and explicit group provenance.
ALTER TABLE autowake_outbox RENAME TO autowake_outbox_legacy_215;

DROP INDEX IF EXISTS idx_autowake_outbox_claim;
DROP INDEX IF EXISTS idx_autowake_outbox_owner_delivery;

ALTER TABLE autowake_message RENAME TO autowake_message_legacy_215;

CREATE TABLE autowake_message (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL
                        REFERENCES autowake_session(id) ON DELETE CASCADE,
    role                TEXT NOT NULL DEFAULT 'assistant'
                        CHECK (role = 'assistant'),
    source_scope        TEXT NOT NULL
                        CHECK (source_scope IN (
                            'owner_direct', 'owner_private', 'derived_owner',
                            'group'
                        )),
    content             TEXT NOT NULL
                        CHECK (length(content) BETWEEN 1 AND 3500),
    created_at          TEXT NOT NULL
);

INSERT INTO autowake_message(
    id, session_id, role, source_scope, content, created_at
)
SELECT id, session_id, role, source_scope, content, created_at
FROM autowake_message_legacy_215;

CREATE TABLE autowake_outbox (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id            INTEGER NOT NULL UNIQUE
                        REFERENCES autowake_event(id) ON DELETE CASCADE,
    session_id          INTEGER NOT NULL
                        REFERENCES autowake_session(id) ON DELETE CASCADE,
    message_id          INTEGER NOT NULL UNIQUE
                        REFERENCES autowake_message(id) ON DELETE CASCADE,
    owner_user_id       INTEGER NOT NULL,
    idempotency_key     TEXT NOT NULL,
    channel             TEXT NOT NULL DEFAULT 'telegram_owner_dm'
                        CHECK (channel IN (
                            'telegram_owner_dm', 'telegram_group'
                        )),
    target_chat_id      INTEGER,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN (
                            'pending', 'leased', 'retry',
                            'delivered', 'dead', 'cancelled'
                        )),
    due_at              TEXT NOT NULL,
    lease_owner         TEXT,
    lease_until         TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts        INTEGER NOT NULL DEFAULT 5
                        CHECK (max_attempts BETWEEN 1 AND 20),
    defer_reason        TEXT,
    last_error_code     TEXT,
    delivered_at        TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    attempt_started_at  TEXT,
    CHECK (
        (status = 'leased' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
        OR
        (status != 'leased' AND lease_owner IS NULL AND lease_until IS NULL)
    ),
    CHECK (
        (channel = 'telegram_owner_dm' AND target_chat_id IS NULL)
        OR
        (channel = 'telegram_group' AND target_chat_id < 0)
    ),
    UNIQUE(owner_user_id, idempotency_key)
);

INSERT INTO autowake_outbox(
    id, event_id, session_id, message_id, owner_user_id, idempotency_key,
    channel, target_chat_id, status, due_at, lease_owner, lease_until,
    attempts, max_attempts, defer_reason, last_error_code, delivered_at,
    created_at, updated_at, attempt_started_at
)
SELECT
    id, event_id, session_id, message_id, owner_user_id, idempotency_key,
    channel, NULL, status, due_at, lease_owner, lease_until,
    attempts, max_attempts, defer_reason, last_error_code, delivered_at,
    created_at, updated_at, attempt_started_at
FROM autowake_outbox_legacy_215;

DROP TABLE autowake_outbox_legacy_215;
DROP TABLE autowake_message_legacy_215;

CREATE INDEX idx_autowake_outbox_claim
    ON autowake_outbox(status, due_at, id);
CREATE INDEX idx_autowake_outbox_owner_delivery
    ON autowake_outbox(owner_user_id, delivered_at);
CREATE INDEX idx_autowake_outbox_group_delivery
    ON autowake_outbox(target_chat_id, delivered_at)
    WHERE channel='telegram_group';
