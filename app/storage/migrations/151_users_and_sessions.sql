-- T2 (v1.66) — Users + auth sessions.
--
-- Persona has always been single-user / local-first. This migration is
-- the first half of moving toward multi-device: a single ``users`` table
-- so devices can later be associated with a person, even though right
-- now there's still only ever going to be one row (the project owner).
--
-- Schema
-- ------
-- ``users``:
--   * ``email`` — lowercased, deduplicated. UNIQUE constraint.
--   * ``password_hash`` — full PBKDF2 record encoded as
--     ``pbkdf2_sha256$ITERATIONS$SALT_HEX$HASH_HEX`` so verify_password
--     can read back the exact parameters used (PBKDF2 is parameterised
--     by iteration count, and we may want to bump iterations over time
--     without invalidating old rows).
--   * ``created_at`` — server time the row was inserted.
--   * ``last_login_at`` — updated on successful login. Optional.
--   * ``display_name`` — optional, used in UI greeting.
--
-- ``auth_session``:
--   * ``token`` — opaque random token (32 bytes hex = 64 chars). Stored
--     as-is (no hash) because session expiry is short and we read this
--     on every authenticated request — hashing would force a slow op on
--     the hot path. Treat the DB file as a credential store.
--   * ``user_id`` — FK to users.id, ON DELETE CASCADE.
--   * ``issued_at`` / ``expires_at`` — ISO timestamps. Default lifetime
--     is 30 days; the route handler can override.
--   * ``revoked_at`` — set to current time when user logs out or when
--     an admin nukes a stolen session. NULL = active.
--   * ``user_agent`` / ``last_seen_at`` — best-effort attribution so
--     a future "your devices / your sessions" UI can show "Chrome on
--     iPhone, last seen 2 hours ago". Both nullable for back-compat.

CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    last_login_at TEXT,
    display_name  TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS auth_session (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    token         TEXT NOT NULL UNIQUE,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    issued_at     TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at    TEXT NOT NULL,
    revoked_at    TEXT,
    user_agent    TEXT,
    last_seen_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_session_token   ON auth_session(token);
CREATE INDEX IF NOT EXISTS idx_auth_session_user    ON auth_session(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_session_active  ON auth_session(revoked_at)
    WHERE revoked_at IS NULL;
