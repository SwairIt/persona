-- T7 (v1.66) — Tombstones for anti-resurrection during sync.
--
-- Problem the table solves:
--   Device A deletes a note at clock=100. The delete event syncs to
--   device B and the row is removed. Some time later device A's earlier
--   "insert" event with clock=50 finally arrives at device B (e.g. the
--   user was offline; events came out of order). Without a tombstone,
--   apply_pending would re-create the row.
--
-- The fix is to remember every delete-by-uuid with the clock at which
-- it happened. apply_pending consults this table before any insert/
-- update and silently skips events older than the tombstone clock.
--
-- Schema notes:
--   * Composite primary key (kind, identity) where ``identity`` is the
--     entity uuid for note / tag / shot, or the kv ``key`` for kv events.
--   * ``clock`` is the largest logical_clock value at which a delete
--     event arrived; subsequent inserts with clock <= this are stale.
--   * ``deleted_at`` is the wall-clock side, useful for the admin UI
--     ("this note was tombstoned 3 days ago").
--   * Rows here are append-only conceptually but UPDATE on clock-bump
--     so the table stays small (one row per ever-deleted entity).

CREATE TABLE IF NOT EXISTS sync_tombstone (
    kind        TEXT NOT NULL,
    identity    TEXT NOT NULL,
    clock       INTEGER NOT NULL DEFAULT 0,
    deleted_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (kind, identity)
);

CREATE INDEX IF NOT EXISTS idx_sync_tombstone_deleted_at
    ON sync_tombstone(deleted_at);
