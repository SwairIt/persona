-- Monthly LLM digest archive (full calendar-month retrospectives, v0.68).

CREATE TABLE IF NOT EXISTS monthly_digest (
    month TEXT PRIMARY KEY,    -- YYYY-MM
    body TEXT NOT NULL,
    provider TEXT,
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
