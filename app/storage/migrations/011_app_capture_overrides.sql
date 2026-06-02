CREATE TABLE IF NOT EXISTS app_capture_overrides (
    app_name TEXT PRIMARY KEY,
    interval_seconds REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
