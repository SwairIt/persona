-- T13 (2026-06-07) — Per-device storage + sync policy.
--
-- Until now every device sees every kind of synced data with no way to
-- say "iPhone, you only need the last 7 days of screenshots" or "Mac,
-- you're the archive — keep everything". Two settings tables solve it:
--
-- ``device_storage_policy``: per-device quota + retention.
--   * ``quota_mb`` — local storage budget. NULL = no limit (Mac archive).
--   * ``retention_days`` — after how many days a shot drops off this
--     device's local cache. NULL = forever. The file stays on the server
--     and can always be re-fetched on demand.
--   * ``role`` — informational tag for the user: "primary" (the main
--     capturing device), "archive" (long-term storage), "viewer"
--     (read-only — phone/iPad), "passive" (sync but no capture).
--     Doesn't change enforcement, but the UI uses it to surface sane
--     defaults: a "viewer" device gets quota_mb=500 retention_days=7,
--     an "archive" device gets nothing capped.
--
-- ``device_sync_filter``: per-(device, kind) opt-out.
--   * Composite PK (device_id, kind) so the same device can mute
--     specific kinds independently.
--   * ``enabled = 0`` means "don't deliver events of this kind on
--     /api/sync/pull". Default (no row) = enabled.
--   * Kinds match the existing ``sync_event.kind`` CHECK set:
--     note, kv, tag, annotation, shot_tag, plus the synthetic
--     ``shot_blob`` reserved for future image-file syncing.

CREATE TABLE IF NOT EXISTS device_storage_policy (
    device_id        INTEGER PRIMARY KEY REFERENCES device(id) ON DELETE CASCADE,
    quota_mb         INTEGER,
    retention_days   INTEGER,
    role             TEXT NOT NULL DEFAULT 'primary'
                     CHECK (role IN ('primary', 'archive', 'viewer', 'passive')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS device_sync_filter (
    device_id  INTEGER NOT NULL REFERENCES device(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (device_id, kind)
);

CREATE INDEX IF NOT EXISTS idx_device_sync_filter_lookup
    ON device_sync_filter(device_id, kind, enabled);
