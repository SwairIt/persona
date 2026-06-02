-- v0.6 auto-digest archive.

CREATE TABLE IF NOT EXISTS daily_digest (
    day TEXT PRIMARY KEY,
    body TEXT NOT NULL,
    provider TEXT,
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
