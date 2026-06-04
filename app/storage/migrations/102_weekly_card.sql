-- v1.15 — tier 2 memory: one weekly summary card per ISO week.
--
-- Compresses 7 daily_pin rows + ~168 hourly_card rows into a single
-- ~5 KB markdown card. Lives forever like daily_pin (no retention sweep
-- ever touches it). Backfills go 12 weeks back so a laptop that was
-- closed for a month catches up on the next worker tick.
--
-- Heuristic-only: computed deterministically from existing tables.

CREATE TABLE IF NOT EXISTS weekly_card (
    week_start TEXT PRIMARY KEY,            -- YYYY-MM-DD (Monday, ISO week)
    week_end TEXT NOT NULL,                 -- YYYY-MM-DD (Sunday)
    summary TEXT NOT NULL,                  -- markdown block, ~5 KB
    top_apps_json TEXT,                     -- JSON array of {app, shots}
    total_screens INTEGER NOT NULL DEFAULT 0,
    total_voice_minutes INTEGER NOT NULL DEFAULT 0,
    days_active INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'heuristic',  -- 'heuristic' | 'llm'
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_weekly_card_week_start
    ON weekly_card(week_start);
