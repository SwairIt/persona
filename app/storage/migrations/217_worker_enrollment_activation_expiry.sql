-- Append-only hardening for the already-applied migration 213.
--
-- Activation has a separate 24-hour window. SQLite cannot add the required
-- cross-column CHECK in place, so rebuild the small enrollment ledger.
ALTER TABLE worker_enrollment_ticket
    RENAME TO worker_enrollment_ticket_legacy_217;

DROP TRIGGER IF EXISTS worker_enrollment_identity_immutable;
DROP INDEX IF EXISTS idx_worker_enrollment_active;
DROP INDEX IF EXISTS idx_worker_enrollment_recent;
DROP INDEX IF EXISTS idx_worker_enrollment_pending_llm;
DROP INDEX IF EXISTS idx_worker_enrollment_pending_browser;
DROP INDEX IF EXISTS idx_worker_enrollment_pending_activation;

CREATE TABLE worker_enrollment_ticket (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_hash         TEXT NOT NULL UNIQUE
                        CHECK (length(ticket_hash) = 64),
    owner_user_id       INTEGER NOT NULL
                        REFERENCES users(id) ON DELETE CASCADE,
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
    pending_llm_token_hash TEXT
                        CHECK (
                            pending_llm_token_hash IS NULL
                            OR length(pending_llm_token_hash) = 64
                        ),
    pending_browser_token_hash TEXT
                        CHECK (
                            pending_browser_token_hash IS NULL
                            OR length(pending_browser_token_hash) = 64
                        ),
    activation_expires_at TEXT,
    activated_at        TEXT,
    revoked_at          TEXT,
    CHECK (
        (status = 'issued'
            AND consumed_at IS NULL
            AND consumed_worker_id IS NULL
            AND pending_llm_token_hash IS NULL
            AND pending_browser_token_hash IS NULL
            AND activation_expires_at IS NULL
            AND activated_at IS NULL
            AND revoked_at IS NULL)
        OR
        (status = 'consumed'
            AND consumed_at IS NOT NULL
            AND consumed_worker_id IS NOT NULL
            AND (
                (
                    pending_llm_token_hash IS NULL
                    AND pending_browser_token_hash IS NULL
                    AND activation_expires_at IS NULL
                    AND activated_at IS NULL
                )
                OR
                (
                    pending_llm_token_hash IS NOT NULL
                    AND pending_browser_token_hash IS NOT NULL
                    AND activation_expires_at IS NOT NULL
                )
            )
            AND revoked_at IS NULL)
        OR
        (status IN ('expired', 'revoked')
            AND consumed_at IS NULL
            AND consumed_worker_id IS NULL
            AND pending_llm_token_hash IS NULL
            AND pending_browser_token_hash IS NULL
            AND activation_expires_at IS NULL
            AND activated_at IS NULL
            AND revoked_at IS NOT NULL)
    )
);

INSERT INTO worker_enrollment_ticket(
    id, ticket_hash, owner_user_id, capability, expected_worker_id,
    status, issued_at, expires_at, consumed_at, consumed_worker_id,
    pending_llm_token_hash, pending_browser_token_hash,
    activation_expires_at, activated_at, revoked_at
)
SELECT
    id, ticket_hash, owner_user_id, capability, expected_worker_id,
    status, issued_at, expires_at, consumed_at, consumed_worker_id,
    pending_llm_token_hash, pending_browser_token_hash,
    CASE
        WHEN pending_llm_token_hash IS NOT NULL
         AND pending_browser_token_hash IS NOT NULL
        THEN datetime(COALESCE(consumed_at, issued_at), '+24 hours')
        ELSE NULL
    END,
    activated_at, revoked_at
FROM worker_enrollment_ticket_legacy_217;

DROP TABLE worker_enrollment_ticket_legacy_217;

CREATE INDEX idx_worker_enrollment_active
    ON worker_enrollment_ticket(owner_user_id, capability, status, expires_at);

CREATE INDEX idx_worker_enrollment_recent
    ON worker_enrollment_ticket(issued_at DESC, id DESC);

CREATE UNIQUE INDEX idx_worker_enrollment_pending_llm
    ON worker_enrollment_ticket(pending_llm_token_hash)
    WHERE pending_llm_token_hash IS NOT NULL;

CREATE UNIQUE INDEX idx_worker_enrollment_pending_browser
    ON worker_enrollment_ticket(pending_browser_token_hash)
    WHERE pending_browser_token_hash IS NOT NULL;

CREATE INDEX idx_worker_enrollment_pending_activation
    ON worker_enrollment_ticket(activation_expires_at)
    WHERE status = 'consumed' AND activated_at IS NULL;

CREATE TRIGGER worker_enrollment_identity_immutable
BEFORE UPDATE OF ticket_hash, owner_user_id, capability, expected_worker_id
ON worker_enrollment_ticket
BEGIN
    SELECT RAISE(ABORT, 'worker enrollment identity is immutable');
END;
