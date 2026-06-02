CREATE TABLE IF NOT EXISTS search_history (
    query TEXT PRIMARY KEY,
    mode TEXT NOT NULL DEFAULT 'hybrid',
    last_used_at TEXT NOT NULL DEFAULT (datetime('now')),
    use_count INTEGER NOT NULL DEFAULT 1
);
