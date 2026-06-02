CREATE TABLE IF NOT EXISTS ocr_skip_app (
    app_name TEXT PRIMARY KEY,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);
