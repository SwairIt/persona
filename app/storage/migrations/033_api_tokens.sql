-- v0.34 — opt-in API bearer tokens for external apps hitting ``/api/*``.
--
-- The application stores **only** the SHA-256 hex digest of each token,
-- never the raw value. ``app.api_tokens.create_token`` returns the raw
-- urlsafe string to the caller *exactly once*; the UI then shows it in
-- a one-time banner. If the operator loses it they must revoke and
-- re-issue — there is no recovery path, by design.
--
-- ``scopes`` is a comma-separated string (``read``, ``read,write``, …).
-- Default is the conservative ``read`` so a bare ``POST /tokens`` form
-- with no scope checked still produces a usable read-only token rather
-- than a write-capable one.
--
-- ``revoked_at`` is the kill switch: ``verify_token`` rejects rows where
-- it is non-NULL even if the hash matches. We never DELETE rows so the
-- audit trail (name / created_at / last_used_at) survives revocation.
--
-- ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` keep
-- the migration idempotent across re-runs of ``init_database``.

CREATE TABLE IF NOT EXISTS api_token (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    scopes TEXT NOT NULL DEFAULT 'read',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_used_at TEXT,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_token_hash ON api_token(token_hash);
