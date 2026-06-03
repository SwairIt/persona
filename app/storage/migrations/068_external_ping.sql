-- v0.74 — idle ping endpoint for external time-tracking scripts.
--
-- Persona owns the screenshot loop, but a user may legitimately want to
-- mark "I was active here even though Persona was paused" — e.g. a CLI
-- focus timer, a build-watch script, a meeting bot. Those tools POST a
-- tiny JSON heartbeat at ``/api/ping`` and we persist one row per beat.
--
-- The schema is deliberately minimal:
--
--   * ``source`` is a free-form short string the external script picks
--     (``cli-timer``, ``ci-watch``, ``zoom-bot``). It is required because
--     every heartbeat needs an owner — anonymous pings would mix together
--     into an un-debuggable pile.
--   * ``label`` is optional and lets the source attach extra context per
--     ping (the project name, the meeting title). Most rows will leave it
--     NULL; that is fine.
--   * ``ts`` defaults to ``datetime('now')`` (UTC, ISO-ish) so a script
--     that only sends ``{"source": "x"}`` still produces a useful row.
--
-- The descending index on ``ts`` mirrors how the admin page reads — the
-- most recent pings are always the interesting ones, so we keep the
-- common path off a full-table scan. SQLite ignores explicit DESC on
-- single-column indexes for ORDER BY purposes but the syntax documents
-- intent and costs nothing.
--
-- ``IF NOT EXISTS`` keeps the migration idempotent across re-runs.

CREATE TABLE IF NOT EXISTS external_ping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    label TEXT,
    ts TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_external_ping_ts_desc
    ON external_ping(ts DESC);
