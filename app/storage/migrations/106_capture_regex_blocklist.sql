-- v1.21 — regex-based capture blocklist.
--
-- Per-app/window-title regex blocklist: the capture loop short-circuits
-- whenever the foreground app name or window title matches one of the
-- enabled patterns. Stricter and more flexible than
-- ``app_capture_skip`` (exact-match by app name only) — the operator
-- can block "any window with 'Bank' in the title", or "anything whose
-- app name starts with 'KeePass'".
--
-- Columns:
--   pattern     — raw regex source (compiled at lookup time, never
--                 mutated by the storage layer).
--   field       — which property of the active window to match against:
--                 'app'  → app_name only
--                 'title'→ window_title only
--                 'both' → either field; first match wins
--   enabled     — soft toggle so the operator can pause a rule without
--                 deleting it.
--   created_at  — ISO-8601 UTC timestamp from SQLite (datetime('now')).
--   description — free-form operator note ("blocks online banking") to
--                 jog memory at audit time. Nullable.

CREATE TABLE IF NOT EXISTS capture_regex_blocklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,
    field TEXT NOT NULL CHECK (field IN ('app','title','both')),
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    description TEXT
);

CREATE INDEX IF NOT EXISTS idx_capture_regex_blocklist_enabled
    ON capture_regex_blocklist(enabled);
