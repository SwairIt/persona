-- Free-text notes attached to screenshots.

CREATE TABLE IF NOT EXISTS screenshot_notes (
    screenshot_id INTEGER PRIMARY KEY REFERENCES screenshots(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_screenshot_notes_updated_at ON screenshot_notes(updated_at);
