-- v0.14 — process-name → app-name remapping + saved-search seen markers.

CREATE TABLE IF NOT EXISTS process_app_remap (
    process_name TEXT PRIMARY KEY,
    app_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

ALTER TABLE saved_searches ADD COLUMN last_seen_screenshot_id INTEGER NOT NULL DEFAULT 0;
ALTER TABLE saved_searches ADD COLUMN last_seen_at TEXT;
