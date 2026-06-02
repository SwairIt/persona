-- Per-screenshot free-form annotations.
--
-- Unlike screenshot_notes (one note per shot, mutable, also indexed by FTS),
-- an "annotation" is an append-only commentary line: you can have many per
-- screenshot, each with its own created_at, and they are NOT part of the
-- note FTS index. Think of them as inline margin scribbles, distinct from
-- the single canonical note and from tags.
--
-- ON DELETE CASCADE so deleting a screenshot wipes its annotations.

CREATE TABLE IF NOT EXISTS screenshot_annotation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    screenshot_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(screenshot_id) REFERENCES screenshots(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_screenshot_annotation_screenshot_id
    ON screenshot_annotation(screenshot_id);
