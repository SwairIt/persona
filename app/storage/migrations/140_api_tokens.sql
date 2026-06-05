-- v1.40 — scoped read-only API tokens for third-party tools.
--
-- The v0.34 ``033_api_tokens.sql`` migration introduced ``api_token`` with
-- the columns Persona's *internal* bearer-auth needs (name, scopes,
-- created_at, last_used_at, revoked_at). This migration extends the same
-- physical table with the bits the v1.40 *admin issuance flow* requires
-- so external integrations get a label-first UX with hard expiry and a
-- usage counter — without breaking the existing 033 helpers.
--
-- Schema mapping
-- --------------
-- * ``label``        — human-readable name from the new admin form. Old
--                      rows minted via 033 already have ``name``; the
--                      new helpers fall back to ``name`` when ``label``
--                      is NULL so the admin table stays populated.
-- * ``expires_at``   — optional hard expiry (UTC ISO-8601). NULL means
--                      "never expires"; ``verify_token`` rejects rows
--                      where ``expires_at < datetime('now')``.
-- * ``use_count``    — incremented on every successful verify so the
--                      admin UI can show "this token is actually in use".
--
-- ``CREATE TABLE IF NOT EXISTS`` keeps this idempotent on fresh installs
-- (the table won't exist yet) AND on installs that already ran 033 (the
-- statement no-ops, and the ALTER TABLEs below add the new columns).
-- ``ALTER TABLE ... ADD COLUMN`` will raise ``duplicate column name`` on
-- re-runs; :func:`app.storage.db._run_migration` catches that and replays
-- the file statement-by-statement, so each ALTER is safe to re-execute.

CREATE TABLE IF NOT EXISTS api_token (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    scopes TEXT NOT NULL DEFAULT 'read',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT,
    revoked_at TEXT,
    last_used_at TEXT,
    use_count INTEGER NOT NULL DEFAULT 0
);

-- Bring rows minted before v1.40 up to the new shape. SQLite raises
-- ``duplicate column name`` if the column already exists; the migration
-- runner treats that as an idempotent no-op.
ALTER TABLE api_token ADD COLUMN label TEXT NOT NULL DEFAULT '';
ALTER TABLE api_token ADD COLUMN expires_at TEXT;
ALTER TABLE api_token ADD COLUMN use_count INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_api_token_hash ON api_token(token_hash);
CREATE INDEX IF NOT EXISTS idx_api_token_revoked
    ON api_token(revoked_at) WHERE revoked_at IS NULL;
