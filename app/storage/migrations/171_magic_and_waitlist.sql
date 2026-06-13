-- T30 фаза2 — passwordless magic-link login + waitlist.
--
-- magic_link: single-use, short-TTL login tokens (see app/auth/magic.py).
-- waitlist:   emails of people who tried to sign up while open registration
--             is gated (app is not yet isolated per-user).

CREATE TABLE IF NOT EXISTS magic_link (
    token       TEXT PRIMARY KEY,
    email       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT NOT NULL,
    used_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_magic_link_email ON magic_link(email);

CREATE TABLE IF NOT EXISTS waitlist (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    email       TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    source      TEXT
);
