-- T15 (2026-06-07) — Server-side storage cleanup audit trail.
--
-- The cleanup worker deletes screenshot files older than the user's
-- retention setting (kv ``shots_retention_days``). We log every run
-- here so the dashboard can show "yesterday's cleanup freed 230 MB,
-- removed 412 shots" without re-scanning the filesystem.
--
-- Single table, append-only. Rows older than 90 days could be GC'd by
-- a future migration but for now we keep everything.

CREATE TABLE IF NOT EXISTS storage_cleanup_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at     TEXT,
    trigger_source  TEXT NOT NULL CHECK (trigger_source IN ('manual', 'worker')),
    shots_deleted   INTEGER NOT NULL DEFAULT 0,
    bytes_freed     INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    -- Snapshot of the policy values in effect during this run, so the
    -- audit row stays meaningful even after the user changes settings.
    retention_days  INTEGER,
    quota_mb        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_storage_cleanup_log_recent
    ON storage_cleanup_log(started_at DESC);
