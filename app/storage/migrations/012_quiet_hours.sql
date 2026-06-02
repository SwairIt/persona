CREATE TABLE IF NOT EXISTS quiet_hours (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weekday INTEGER NOT NULL,        -- 0 = Mon, 6 = Sun (Python's weekday())
    start_hour INTEGER NOT NULL,     -- 0-23
    end_hour INTEGER NOT NULL,       -- 0-24 (24 means end of day)
    label TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_quiet_hours_weekday ON quiet_hours(weekday);
