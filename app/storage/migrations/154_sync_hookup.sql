-- T4 hook-up (v1.66) — Wire sync_event into real canonical tables.
--
-- The T4 substrate (migration 153) ships an append-only event log plus a
-- pull/push API. This migration adds the columns ``apply_pending`` needs
-- to materialise those events back into the existing tables:
--
-- ``notes.uuid``
--   A stable per-row identifier that survives crossing devices. The
--   numeric ``id`` column is autoincrement-per-DB and therefore not
--   comparable between two SQLite files. Existing rows get NULL — they
--   only become syncable when the user explicitly migrates them via the
--   /sync admin page (planned). New rows get a UUID generated at insert
--   time. UNIQUE is enforced via the index — the table itself stays
--   nullable for back-compat.
--
-- ``kv_settings.last_applied_clock``
--   Lamport-style monotonic clock per kv key. When a sync event tries
--   to update a key, apply_pending compares the event's ``logical_clock``
--   against this column and ignores stale writes. NULL → treat as 0.

ALTER TABLE notes ADD COLUMN uuid TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_notes_uuid ON notes(uuid)
    WHERE uuid IS NOT NULL;

ALTER TABLE kv_settings ADD COLUMN last_applied_clock INTEGER NOT NULL DEFAULT 0;
