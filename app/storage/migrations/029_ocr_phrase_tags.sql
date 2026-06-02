CREATE TABLE IF NOT EXISTS ocr_phrase_tag (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase TEXT NOT NULL,
    tag TEXT NOT NULL,
    case_sensitive INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(phrase, tag)
);
