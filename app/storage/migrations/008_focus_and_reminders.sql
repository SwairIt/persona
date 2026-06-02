-- v0.10 — focus sessions, reminders, reading list.

CREATE TABLE IF NOT EXISTS focus_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    duration_minutes INTEGER NOT NULL,
    intent TEXT,
    outcome TEXT,
    completed INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_focus_sessions_started ON focus_sessions(started_at);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    body TEXT NOT NULL,
    due_date TEXT NOT NULL,
    done INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(due_date);

CREATE TABLE IF NOT EXISTS reading_list (
    screenshot_id INTEGER PRIMARY KEY REFERENCES screenshots(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    read_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reading_list_added ON reading_list(added_at);
