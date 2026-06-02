-- Tagging / pinning of screenshots.

CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    color TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS screenshot_tags (
    screenshot_id INTEGER NOT NULL REFERENCES screenshots(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (screenshot_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_screenshot_tags_tag ON screenshot_tags(tag_id);

CREATE TABLE IF NOT EXISTS saved_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    query TEXT NOT NULL,
    app_name TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_saved_searches_name ON saved_searches(name);
