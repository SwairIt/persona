-- v1.51 — On-demand / scheduled SQLite integrity quick-check + ANALYZE.
--
-- SQLite never corrupts itself under normal use, but the operator runs
-- this database on a laptop with a fan-out of weird things (cloud
-- syncing, sleep/wake, the occasional ``kill -9`` of the daemon). A
-- daily ``PRAGMA quick_check`` catches torn pages and free-list damage
-- before they bite a downstream feature; a paired ``ANALYZE`` keeps the
-- query planner's stats fresh after the bulk imports the long-read
-- pipeline does overnight.
--
-- This table is the bookkeeping side: one row per check, archived for
-- the UI so the operator can see "everything was ok every day this
-- month" at a glance. ``full_check`` is a separate kind because
-- ``PRAGMA integrity_check`` rewalks every page and is too slow for
-- daily use — the worker only fires ``quick_check`` + ``analyze``; the
-- "Run Full Check" button on the admin page is the only producer of
-- ``check_kind = 'full'`` rows.
--
-- Schema notes:
--   ran_at         — wall-clock ISO-8601 from SQLite's ``datetime('now')``.
--                    Matches the rest of Persona's timestamp shape so a
--                    corrupt clock surfaces the same way everywhere.
--   check_kind     — 'quick' (PRAGMA quick_check), 'full' (PRAGMA
--                    integrity_check), or 'analyze' (PRAGMA optimize +
--                    ANALYZE). CHECK constraint keeps junk out.
--   result         — the raw text PRAGMA returned. For a healthy DB
--                    quick_check/integrity_check return literally ``ok``;
--                    for ANALYZE we store ``ok`` on success or the
--                    error message on failure. Long results (multiple
--                    error lines) are stored verbatim — the UI can
--                    truncate, but forensics shouldn't have to guess.
--   duration_ms    — how long the PRAGMA + INSERT round-trip took, in
--                    milliseconds. Lets the operator notice the DB
--                    getting slower over time.
--   db_size_bytes  — ``page_count * page_size`` at the moment the check
--                    ran. DEFAULT 0 so a future code path that pre-
--                    creates the row and patches the size later still
--                    parses.
--
-- Indices:
--   idx_db_integrity_run_ran — the admin page is always "give me the
--                    last N runs"; the timestamp column carries the
--                    index. Not DESC — SQLite scans either direction
--                    efficiently and the migrations file stays portable.

CREATE TABLE IF NOT EXISTS db_integrity_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ran_at TEXT NOT NULL DEFAULT (datetime('now')),
    check_kind TEXT NOT NULL CHECK (check_kind IN ('quick', 'full', 'analyze')),
    result TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    db_size_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_db_integrity_run_ran ON db_integrity_run(ran_at);
