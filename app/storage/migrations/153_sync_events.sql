-- T4 (v1.66) — Sync event log for multi-device local-first replication.
--
-- Design note: this is NOT a CRDT implementation. It is the event-source
-- substrate a CRDT can later sit on top of without re-doing the schema.
-- For now the merge strategy is last-write-wins per (entity_kind,
-- entity_id, field) keyed by ``logical_clock``. Notes and annotations
-- can be upgraded to Yjs/Automerge later by writing the encoded CRDT
-- delta into ``payload_json`` instead of the plain field map.
--
-- Schema
-- ------
-- ``sync_event``:
--   * ``id``                — monotonic server-side id. Devices pull events
--                              "since id=N" to get everything after their
--                              own watermark.
--   * ``device_id``         — origin device. NULL when the event was
--                              produced by the central server itself
--                              (e.g. an admin-triggered rename).
--   * ``user_id``           — required for scoping. A device's events
--                              only sync to the same user's other devices.
--   * ``kind``              — short slug naming the entity touched:
--                              'note' | 'tag' | 'annotation' | 'kv' | 'pin'.
--                              Future kinds: 'shot_delete', 'reaction', …
--   * ``entity_id``         — primary key of the touched row in its
--                              kind-specific table. NULL for 'kv' (where
--                              the key lives in payload_json).
--   * ``op``                — 'insert' | 'update' | 'delete'.
--   * ``payload_json``      — the full new field state (for inserts/updates)
--                              or {} (for deletes). Stored as TEXT.
--   * ``logical_clock``     — device-supplied monotonic counter, used for
--                              last-write-wins reconciliation when two
--                              devices race on the same (kind, entity_id).
--   * ``server_recv_at``    — when the central server got the event.
--                              Used for "events since timestamp X" pulls
--                              when devices lose their watermark.
--   * ``applied_at``        — when the server applied the event to the
--                              canonical tables. NULL = still pending.
--
-- ``device_sync_state``:
--   * Tracks per-device watermark: the largest sync_event.id the device
--     has acknowledged. Devices pull from id > last_pulled_id.
--   * Devices also report their largest pushed logical_clock so the
--     server can de-dup re-tries.

CREATE TABLE IF NOT EXISTS sync_event (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id       INTEGER REFERENCES device(id) ON DELETE SET NULL,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    entity_id       INTEGER,
    op              TEXT NOT NULL CHECK (op IN ('insert', 'update', 'delete')),
    payload_json    TEXT NOT NULL DEFAULT '{}',
    logical_clock   INTEGER NOT NULL DEFAULT 0,
    server_recv_at  TEXT NOT NULL DEFAULT (datetime('now')),
    applied_at      TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_event_user    ON sync_event(user_id);
CREATE INDEX IF NOT EXISTS idx_sync_event_kind    ON sync_event(user_id, kind);
CREATE INDEX IF NOT EXISTS idx_sync_event_pending ON sync_event(applied_at)
    WHERE applied_at IS NULL;

CREATE TABLE IF NOT EXISTS device_sync_state (
    device_id            INTEGER PRIMARY KEY REFERENCES device(id) ON DELETE CASCADE,
    last_pulled_event_id INTEGER NOT NULL DEFAULT 0,
    last_pushed_clock    INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
