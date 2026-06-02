-- Saved search bookmarks — explicit user-pinned queries.
-- Distinct from `search_history` (016) which auto-tracks recently used queries:
-- bookmarks live until the user deletes them and have a human-readable title.
CREATE TABLE IF NOT EXISTS saved_search (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    query TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_saved_search_created_at
    ON saved_search (created_at DESC);
