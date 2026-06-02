-- Persona v0 schema. Run on first init; idempotent.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS dedup_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    representative_screenshot_id INTEGER,
    phash TEXT NOT NULL,
    seen_count INTEGER NOT NULL DEFAULT 1,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_dedup_groups_phash ON dedup_groups(phash);
CREATE INDEX IF NOT EXISTS idx_dedup_groups_last_seen ON dedup_groups(last_seen);

CREATE TABLE IF NOT EXISTS screenshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL,
    monitor_index INTEGER NOT NULL DEFAULT 0,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    thumbnail_path TEXT,
    phash TEXT NOT NULL,
    app_name TEXT,
    window_title TEXT,
    process_name TEXT,
    ocr_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (ocr_status IN ('pending', 'done', 'skipped', 'failed')),
    ocr_text TEXT,
    dedup_group_id INTEGER REFERENCES dedup_groups(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_screenshots_captured_at ON screenshots(captured_at);
CREATE INDEX IF NOT EXISTS idx_screenshots_app_name ON screenshots(app_name);
CREATE INDEX IF NOT EXISTS idx_screenshots_ocr_status ON screenshots(ocr_status);
CREATE INDEX IF NOT EXISTS idx_screenshots_dedup_group ON screenshots(dedup_group_id);

CREATE TABLE IF NOT EXISTS capture_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    event_type TEXT NOT NULL
        CHECK (event_type IN ('start', 'pause', 'resume', 'error', 'heartbeat', 'cleanup')),
    details TEXT
);

CREATE INDEX IF NOT EXISTS idx_capture_events_ts ON capture_events(ts);
CREATE INDEX IF NOT EXISTS idx_capture_events_type ON capture_events(event_type);

CREATE TABLE IF NOT EXISTS kv_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE VIRTUAL TABLE IF NOT EXISTS screenshots_fts USING fts5(
    ocr_text,
    window_title,
    app_name,
    content='screenshots',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS screenshots_ai AFTER INSERT ON screenshots
BEGIN
    INSERT INTO screenshots_fts(rowid, ocr_text, window_title, app_name)
    VALUES (new.id, COALESCE(new.ocr_text, ''), COALESCE(new.window_title, ''), COALESCE(new.app_name, ''));
END;

CREATE TRIGGER IF NOT EXISTS screenshots_ad AFTER DELETE ON screenshots
BEGIN
    INSERT INTO screenshots_fts(screenshots_fts, rowid, ocr_text, window_title, app_name)
    VALUES ('delete', old.id, COALESCE(old.ocr_text, ''), COALESCE(old.window_title, ''), COALESCE(old.app_name, ''));
END;

CREATE TRIGGER IF NOT EXISTS screenshots_au AFTER UPDATE ON screenshots
BEGIN
    INSERT INTO screenshots_fts(screenshots_fts, rowid, ocr_text, window_title, app_name)
    VALUES ('delete', old.id, COALESCE(old.ocr_text, ''), COALESCE(old.window_title, ''), COALESCE(old.app_name, ''));
    INSERT INTO screenshots_fts(rowid, ocr_text, window_title, app_name)
    VALUES (new.id, COALESCE(new.ocr_text, ''), COALESCE(new.window_title, ''), COALESCE(new.app_name, ''));
END;
