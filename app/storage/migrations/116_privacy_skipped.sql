-- v1.40 — privacy-mode silent skip log.
--
-- The capture loop short-circuits BEFORE persisting any metadata row
-- when the active window matches a privacy sentinel pattern (Chrome
-- Incognito, Firefox Private Browsing, password managers, banking
-- apps, etc.). Stricter than ``capture_regex_blocklist``: that table
-- still emits a ``capture.blocked_by_regex`` log line that names the
-- window title; this path never records the title text — only a
-- truncated SHA-256 of it, so an audit can prove a skip happened
-- without leaking what the user was looking at.
--
-- Columns:
--   skipped_at         — ISO-8601 UTC stamp via SQLite ``datetime('now')``.
--   pattern_matched    — which sentinel pattern fired (e.g. ``Incognito``).
--                        Nullable defensively — a future caller may want
--                        to record "matched something" without naming
--                        the pattern.
--   app_name           — active app name at the skip moment. Plain text
--                        (low-cardinality, already stored elsewhere) so
--                        the admin page can group by it.
--   window_title_hash  — sha256 hex (first 16 chars) of the window title.
--                        We never store the title text itself: even a
--                        truncated raw title from a banking site can
--                        leak account names.

CREATE TABLE IF NOT EXISTS privacy_skip_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skipped_at TEXT NOT NULL DEFAULT (datetime('now')),
    pattern_matched TEXT,
    app_name TEXT,
    window_title_hash TEXT
);

CREATE INDEX IF NOT EXISTS idx_privacy_skip_at
    ON privacy_skip_event(skipped_at);
