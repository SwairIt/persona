-- v0.37 — worker heartbeat tracking.
--
-- Each background worker upserts a row into ``worker_heartbeat`` at the
-- top of every loop iteration. The ``/admin/health`` dashboard reads
-- this table to render uptime + freshness for every worker and surface
-- the same data as JSON at ``/api/health.json`` for external probes.
--
-- ``ticks`` is the count of beat() calls — a fast, cheap "is the loop
-- making progress?" signal that requires no extra book-keeping per
-- iteration. ``last_status`` carries an opaque short string (``ok`` /
-- ``error`` / ``idle`` / ``disabled``) the worker chooses.
--
-- ``IF NOT EXISTS`` keeps the migration idempotent across re-runs.

CREATE TABLE IF NOT EXISTS worker_heartbeat (
    name TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL,
    last_status TEXT,
    ticks INTEGER NOT NULL DEFAULT 0
);
