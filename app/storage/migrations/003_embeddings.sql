-- Local semantic-search index.
-- Each screenshot gets one fixed-dim float32 vector stored as a BLOB.

CREATE TABLE IF NOT EXISTS screenshot_embeddings (
    screenshot_id INTEGER PRIMARY KEY REFERENCES screenshots(id) ON DELETE CASCADE,
    model TEXT NOT NULL,
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL,
    text_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_screenshot_embeddings_model ON screenshot_embeddings(model);
