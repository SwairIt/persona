CREATE TABLE IF NOT EXISTS regex_auto_tag_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,
    tag_name TEXT NOT NULL,
    case_insensitive INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_matched_at TEXT,
    match_count INTEGER NOT NULL DEFAULT 0
);
