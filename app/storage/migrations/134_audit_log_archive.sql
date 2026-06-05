-- v1.48 — Audit-log rotation + gzip archive bookkeeping.
--
-- ``audit_log`` accumulates one row per destructive admin action since
-- v0.36 and never gets pruned. On a long-running install (months,
-- years) the table balloons and the ``idx_audit_log_ts`` index — the
-- one the security review UI uses — slows enough to be felt.
--
-- The fix is two-sided: a worker (``audit_log_rotation_worker``) wakes
-- once per day, and when the row count exceeds the ``keep_rows`` budget
-- (default 5000) it dumps the oldest excess into a gzipped JSONL file
-- under ``~/.persona/audit-archives/`` and DELETEs them from the live
-- table. The archive files are append-only on disk and never read by
-- the worker again — they're there for compliance / forensics.
--
-- This table records each archive run so the operator UI can show
-- "what got archived, when, how big". One row per successful run; rows
-- with ``rows_archived = 0`` are NOT inserted (the worker simply
-- returns ``not_needed`` when nothing was over the threshold).
--
-- Schema notes:
--   archived_at      — wall-clock ISO-8601 from SQLite's ``datetime('now')``.
--                      Matches the rest of Persona's timestamp shape so
--                      a corrupt clock surfaces the same way everywhere.
--   oldest_row_at    — ``ts`` of the *oldest* row included in this batch
--                      (NULL on the degenerate empty-batch case, which
--                      we don't insert today but the column tolerates).
--   newest_row_at    — ``ts`` of the *newest* row included; together
--                      with ``oldest_row_at`` it brackets the JSONL file
--                      contents without re-reading it.
--   rows_archived    — count of rows moved to disk in this run.
--   file_path        — absolute path to the gzipped JSONL on disk.
--                      Stored absolute (not relative to ``data_dir``)
--                      because the archive directory is configurable per
--                      run via the ``archive_dir`` kwarg and may live
--                      outside the data tree on operator request.
--   file_size_bytes  — gzipped size on disk for the UI list. DEFAULT 0
--                      so a future code path that pre-creates the row
--                      and patches the size later still parses.

CREATE TABLE IF NOT EXISTS audit_log_archive_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archived_at TEXT NOT NULL DEFAULT (datetime('now')),
    oldest_row_at TEXT,
    newest_row_at TEXT,
    rows_archived INTEGER NOT NULL,
    file_path TEXT NOT NULL,
    file_size_bytes INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_audit_log_archive_run_at
    ON audit_log_archive_run(archived_at DESC);
