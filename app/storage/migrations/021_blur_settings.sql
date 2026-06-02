-- v0.25: physical pixel blur of OCR-detected sensitive regions.
--
-- Counterpart to v0.24's text-only redaction (table redaction_rule). Once
-- the OCR worker locates word bounding boxes that match an enabled
-- redaction pattern, app/image_blur.py overwrites those pixels with a
-- Gaussian blur on the on-disk file.  We record the audit trail in a
-- companion table rather than ALTER-ing screenshots, because SQLite has
-- no IF NOT EXISTS for columns and a sibling table is fully idempotent.
CREATE TABLE IF NOT EXISTS blur_applied (
    screenshot_id INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now')),
    regions_count INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (screenshot_id) REFERENCES screenshots(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_blur_applied_applied_at
    ON blur_applied(applied_at);
