-- v0.11 — private vault for sensitive screenshots.

ALTER TABLE screenshots ADD COLUMN is_private INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_screenshots_private ON screenshots(is_private);

CREATE TABLE IF NOT EXISTS private_vault (
    screenshot_id INTEGER PRIMARY KEY REFERENCES screenshots(id) ON DELETE CASCADE,
    encrypted_payload BLOB NOT NULL,
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
