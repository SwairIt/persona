-- v1.14 — tier 5 memory: one ultra-compact daily pin per day, FOREVER.
--
-- Even after aggressive long-term compression removes raw thumbnails,
-- audio segments, hourly cards, daily digests, the daily_pin row stays.
-- It's tiny (~200 bytes plain text) so a 10-year archive is ~750 KB.
--
-- Intentionally NOT included in any retention sweep. The user can
-- always answer "what did I do roughly on 2026-06-04?" from the pin
-- alone, even when every other tier has been purged.
--
-- Format: free-text markdown, ≤500 chars, populated by the day-end
-- summary scheduler (or recomputed deterministically from the day's
-- hourly cards if LLM is unavailable).

CREATE TABLE IF NOT EXISTS daily_pin (
    day TEXT PRIMARY KEY,                   -- YYYY-MM-DD (local TZ)
    pin TEXT NOT NULL,                      -- the micro-summary itself
    apps TEXT,                              -- ~5 top apps comma-separated
    voice_minutes INTEGER NOT NULL DEFAULT 0,
    screen_count INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'heuristic',  -- 'heuristic' | 'llm'
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_daily_pin_created
    ON daily_pin(created_at);
