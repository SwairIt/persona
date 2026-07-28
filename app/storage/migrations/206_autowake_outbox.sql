-- Durable owner-only proactive messaging and Telegram outbox.
--
-- Event metadata is retained for rejected privacy decisions, but unsafe
-- message content never enters autowake_message/autowake_outbox.  A delivery
-- target is intentionally absent: the transport adapter resolves only the
-- configured owner's private Telegram chat.
CREATE TABLE IF NOT EXISTS autowake_event (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id       INTEGER NOT NULL,
    kind                TEXT NOT NULL,
    source              TEXT NOT NULL,
    source_scope        TEXT NOT NULL
                        CHECK (source_scope IN (
                            'owner_direct', 'owner_private', 'derived_owner',
                            'group', 'external', 'secret'
                        )),
    idempotency_key     TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL,
    status              TEXT NOT NULL
                        CHECK (status IN (
                            'queued', 'rejected', 'delivered', 'dead'
                        )),
    rejection_reason    TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE(owner_user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS autowake_session (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_user_id       INTEGER NOT NULL,
    trigger_event_id    INTEGER NOT NULL UNIQUE
                        REFERENCES autowake_event(id) ON DELETE CASCADE,
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK (status IN ('queued', 'delivered', 'dead')),
    created_at          TEXT NOT NULL,
    finished_at         TEXT
);

CREATE TABLE IF NOT EXISTS autowake_message (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          INTEGER NOT NULL
                        REFERENCES autowake_session(id) ON DELETE CASCADE,
    role                TEXT NOT NULL DEFAULT 'assistant'
                        CHECK (role = 'assistant'),
    source_scope        TEXT NOT NULL
                        CHECK (source_scope IN (
                            'owner_direct', 'owner_private', 'derived_owner'
                        )),
    content             TEXT NOT NULL
                        CHECK (length(content) BETWEEN 1 AND 3500),
    created_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS autowake_outbox (
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
                        CHECK (channel = 'telegram_owner_dm'),
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
    CHECK (
        (status = 'leased' AND lease_owner IS NOT NULL AND lease_until IS NOT NULL)
        OR
        (status != 'leased' AND lease_owner IS NULL AND lease_until IS NULL)
    ),
    UNIQUE(owner_user_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_autowake_outbox_claim
    ON autowake_outbox(status, due_at, id);
CREATE INDEX IF NOT EXISTS idx_autowake_outbox_owner_delivery
    ON autowake_outbox(owner_user_id, delivered_at);
CREATE INDEX IF NOT EXISTS idx_autowake_event_status
    ON autowake_event(status, created_at);
