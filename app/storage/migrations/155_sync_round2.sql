-- T5 round 2 (v1.66) — Tag sync + per-kind push clock dedup.
--
-- Why per-kind clock:
--   T4 used a single ``device_sync_state.last_pushed_clock`` field per
--   device — a global Lamport counter for ALL kinds. End-to-end testing
--   showed this bricks legitimate pushes: after the device pushes a
--   note with clock=100, any kv push with clock<100 is silently rejected
--   even though kv and note clocks are independent.
--
--   Fix: a per-kind clock map in its own table. The composite primary
--   key (device_id, kind) caps row count to ~5 rows per device.
--
-- Why tags.uuid:
--   The natural identity of a tag is its ``name`` (already UNIQUE by
--   convention in the codebase). But we add ``uuid`` so the sync
--   handler has a single identifier for INSERT/UPDATE/DELETE — a tag
--   rename event needs to find the row by SOMETHING other than its
--   (now-stale) name. Without uuid the rename becomes "delete old +
--   insert new", which loses ``color`` and ``created_at``.

ALTER TABLE tags ADD COLUMN uuid TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_uuid ON tags(uuid)
    WHERE uuid IS NOT NULL;

CREATE TABLE IF NOT EXISTS device_push_clock (
    device_id   INTEGER NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    high_clock  INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (device_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_device_push_clock_device ON device_push_clock(device_id);
