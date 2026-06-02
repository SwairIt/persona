-- User-starred screenshots for quick recall.
--
-- Distinct from the "pinned" tier (migration 004): pin freezes a row so the
-- tier sweep never demotes it, whereas a "favourite" is purely a discovery
-- shortcut — "I want to find this fast later". A shot can be one, both, or
-- neither. The table is a thin one-to-one link to `screenshots`.
--
-- ON DELETE CASCADE so removing a screenshot also drops the favourite.

CREATE TABLE IF NOT EXISTS favourite (
    screenshot_id INTEGER PRIMARY KEY,
    starred_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(screenshot_id) REFERENCES screenshots(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_favourite_starred_at
    ON favourite (starred_at DESC);
