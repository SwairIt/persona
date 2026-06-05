-- v1.41 — Capture-rate adaptive learning advisor.
--
-- Persona's capture loop has two main knobs that govern daily byte
-- usage: ``capture_interval_seconds`` (how often a frame is grabbed)
-- and ``dedup_hamming_threshold`` (how aggressively near-duplicate
-- frames are folded together before they reach disk). Both of them
-- ship with reasonable defaults, but the "right" value depends on
-- workload: a developer staring at a static editor most of the day
-- produces dramatically less dedup-defeating motion than a designer
-- panning around Figma, even at identical interval settings.
--
-- The rate-advisor is a background analyser that looks at the recent
-- daily_budget_state history (migration 096), averages the per-bucket
-- byte totals across thumbnails+audio+events for the last ``N`` days
-- (default 7), and compares the average against the configured
-- ``daily_budget_mb`` cap. From that single ratio it derives a
-- suggested new (interval, dedup-threshold) pair plus a short
-- human-readable rationale. The suggestion is NOT auto-applied — the
-- operator reviews it on /settings/rate-advisor and clicks Apply, at
-- which point the kv_settings rows are bumped and ``applied_at`` is
-- stamped on the row so we can audit a posteriori which suggestions
-- the operator trusted.
--
-- Column contract
-- ---------------
--   * ``run_at`` — UTC wall-clock when the advisor was run.
--   * ``avg_daily_mb`` — observed mean across the lookback window.
--   * ``cap_mb`` — the daily_budget_mb cap at the time of the run.
--   * ``current_*`` — the live values of the two knobs as the advisor
--     read them (NOT what they were when the suggestion is later
--     applied — those may have drifted in the interim).
--   * ``suggested_*`` — the proposed new values.
--   * ``rationale`` — a short JSON/text blurb the UI renders verbatim.
--   * ``applied_at`` — NULL until the operator clicks Apply; the
--     route stamps datetime('now') and leaves the row otherwise alone.
--
-- The table is tiny (one row per manual or scheduled run, expected
-- cadence ~weekly) so no indexes besides the implicit PK are needed.

CREATE TABLE IF NOT EXISTS rate_advisor_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL DEFAULT (datetime('now')),
    avg_daily_mb REAL NOT NULL,
    cap_mb REAL NOT NULL,
    current_interval_seconds REAL NOT NULL,
    current_dedup_threshold INTEGER NOT NULL,
    suggested_interval_seconds REAL NOT NULL,
    suggested_dedup_threshold INTEGER NOT NULL,
    rationale TEXT,
    applied_at TEXT
);
