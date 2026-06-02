-- Weekly LLM digest archive (Mon-Sun retrospectives).

CREATE TABLE IF NOT EXISTS weekly_digest (
    week_start TEXT PRIMARY KEY,    -- YYYY-MM-DD of the Monday
    body TEXT NOT NULL,
    provider TEXT,
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
