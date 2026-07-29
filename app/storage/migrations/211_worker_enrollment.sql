-- One-use, short-lived enrollment for the owner's outbound PC workers.
--
-- Only a SHA-256 digest of the plaintext ticket is durable.  Consuming a
-- ticket and rotating the two independent scoped worker credentials happens
-- in one BEGIN IMMEDIATE transaction in the application adapter.
CREATE TABLE IF NOT EXISTS worker_enrollment_ticket (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_hash         TEXT NOT NULL UNIQUE
                        CHECK (length(ticket_hash) = 64),
    owner_user_id       INTEGER NOT NULL
                        REFERENCES users(id) ON DELETE RESTRICT,
    capability          TEXT NOT NULL DEFAULT 'llm+browser'
                        CHECK (capability = 'llm+browser'),
    expected_worker_id  TEXT,
    status              TEXT NOT NULL DEFAULT 'issued'
                        CHECK (status IN (
                            'issued', 'consumed', 'expired', 'revoked'
                        )),
    issued_at           TEXT NOT NULL,
    expires_at          TEXT NOT NULL,
    consumed_at         TEXT,
    consumed_worker_id  TEXT,
    revoked_at          TEXT,
    CHECK (
        (status = 'issued'
            AND consumed_at IS NULL
            AND consumed_worker_id IS NULL
            AND revoked_at IS NULL)
        OR
        (status = 'consumed'
            AND consumed_at IS NOT NULL
            AND consumed_worker_id IS NOT NULL
            AND revoked_at IS NULL)
        OR
        (status IN ('expired', 'revoked')
            AND consumed_at IS NULL
            AND consumed_worker_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_worker_enrollment_active
    ON worker_enrollment_ticket(owner_user_id, capability, status, expires_at);

CREATE INDEX IF NOT EXISTS idx_worker_enrollment_recent
    ON worker_enrollment_ticket(issued_at DESC, id DESC);

CREATE TRIGGER IF NOT EXISTS worker_enrollment_identity_immutable
BEFORE UPDATE OF ticket_hash, owner_user_id, capability, expected_worker_id
ON worker_enrollment_ticket
BEGIN
    SELECT RAISE(ABORT, 'worker enrollment identity is immutable');
END;
