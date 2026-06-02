CREATE TABLE IF NOT EXISTS share_collections (
    token TEXT PRIMARY KEY,
    title TEXT,
    screenshot_ids TEXT NOT NULL,        -- JSON list of integers
    expires_unix INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_share_collections_expires ON share_collections(expires_unix);
