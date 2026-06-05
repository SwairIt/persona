-- v1.43 — user-editable privacy-bundle preset library.
--
-- The privacy-mode sentinel (``app.privacy_mode``) historically shipped
-- a single hard-coded ``PRIVACY_PATTERNS`` tuple. v1.43 extends that
-- design with *bundles*: a grouped, named set of patterns the operator
-- can install from preset cards (incognito browsing, password managers,
-- banking, crypto wallets, dating apps, mental-health apps) or compose
-- from scratch. The hard-coded tuple stays as a back-compat fallback
-- when the DB read fails; bundle patterns are appended on top.
--
-- Schema notes:
--   privacy_bundle.name    — UNIQUE so ``install_preset`` can ON CONFLICT
--                            DO NOTHING without an explicit existence
--                            check (single statement = no race).
--   privacy_bundle.enabled — soft toggle (1=on, 0=off). The compile
--                            cache filters on this flag, so an operator
--                            can pause a bundle without dropping its
--                            patterns.
--   privacy_bundle_pattern — child rows; ON DELETE CASCADE means
--                            removing a bundle removes its patterns in
--                            the same transaction.
--   idx_privacy_bundle_enabled — the hot path (privacy_mode compile
--                                cache) filters on enabled=1 every
--                                fingerprint refresh.

CREATE TABLE IF NOT EXISTS privacy_bundle (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS privacy_bundle_pattern (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bundle_id INTEGER NOT NULL REFERENCES privacy_bundle(id) ON DELETE CASCADE,
    pattern TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_privacy_bundle_enabled
    ON privacy_bundle(enabled);
