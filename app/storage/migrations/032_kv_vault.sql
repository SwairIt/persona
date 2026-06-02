-- v0.33 — encrypted key/value vault for BYO API keys & other secrets.
--
-- Unlike ``kv_settings`` (plain text rows: SMTP host, theme, etc.) this
-- table stores **per-row Fernet ciphertext** keyed by a stable string
-- identifier. The user supplies a master password at write time; the
-- key-derivation salt is prepended to the ciphertext blob, so each row
-- is independently decryptable and rotating one secret never forces a
-- rewrite of the others.
--
-- The schema is intentionally minimal — no ``updated_at``, no metadata,
-- nothing the user would have to re-enter if the wrapping format ever
-- changes. The application layer in :mod:`app.vault` owns the entire
-- envelope: salt + Fernet token. SQLite only sees opaque bytes.
--
-- ``CREATE TABLE IF NOT EXISTS`` keeps the migration idempotent across
-- re-runs of :func:`app.storage.db.init_database`.

CREATE TABLE IF NOT EXISTS kv_vault (
    key TEXT PRIMARY KEY,
    ciphertext BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
