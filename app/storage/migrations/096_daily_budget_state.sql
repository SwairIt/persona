-- v1.13 — Storage-budget enforcer per-day bucket accounting.
--
-- One row per UTC day. The capture loop and audio worker bump the
-- relevant bucket on every write; a background sampler reconciles
-- against the actual on-disk total once an hour so drift between the
-- counter and the file system stays bounded.
--
-- ``throttle_level`` is 0=normal, 1=mild, 2=strict, 3=emergency.
-- Transitions are debounced (5-min cooldown) in the worker so a single
-- noisy hour doesn't flap the level repeatedly. See
-- docs/STORAGE_BUDGET_DESIGN.md §6 for the full state machine.

CREATE TABLE IF NOT EXISTS daily_budget_state (
    day TEXT PRIMARY KEY,
    thumbnails_bytes INTEGER NOT NULL DEFAULT 0,
    audio_bytes INTEGER NOT NULL DEFAULT 0,
    events_bytes INTEGER NOT NULL DEFAULT 0,
    ocr_text_bytes INTEGER NOT NULL DEFAULT 0,
    embeddings_bytes INTEGER NOT NULL DEFAULT 0,
    misc_bytes INTEGER NOT NULL DEFAULT 0,
    throttle_level INTEGER NOT NULL DEFAULT 0,
    last_updated TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_daily_budget_state_updated
    ON daily_budget_state(last_updated);
