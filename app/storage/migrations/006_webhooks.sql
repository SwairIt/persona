-- v0.9 outbound webhooks.

CREATE TABLE IF NOT EXISTS webhooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL,
    event_type TEXT NOT NULL,
    secret TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_delivered_at TEXT,
    last_status_code INTEGER,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_webhooks_event ON webhooks(event_type);
