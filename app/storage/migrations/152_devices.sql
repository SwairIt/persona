-- T3 (v1.66) — Devices table.
--
-- A device is anything that captures into Persona on behalf of a user:
-- the Mac agent on someone's laptop, the iPhone PWA, a Windows companion
-- running headless. Multiple devices may belong to one user and the
-- dashboard at /devices is where the user sees them all and toggles
-- per-device settings remotely.
--
-- Schema
-- ------
-- ``device``:
--   * ``user_id`` — FK to users.id, ON DELETE CASCADE.
--   * ``name`` — human label ("Yaroslav's MacBook", "iPhone 15 Pro").
--   * ``kind`` — short slug: 'mac' | 'iphone' | 'windows' | 'web' | 'other'.
--   * ``device_token`` — opaque secret the device sends with every
--     heartbeat/capture call. Stored in plaintext for fast lookup on the
--     hot path; rotated by /devices UI when a device is lost.
--   * ``capture_paused`` — remote toggle. The device's local capture-loop
--     reads this on its next heartbeat; when 1, it skips iteration.
--   * ``capture_interval_seconds`` — remote override of the device-local
--     setting. NULL = let device use its own setting.
--   * ``created_at`` / ``last_seen_at`` — for the "Last active 5 min ago"
--     line in the dashboard.
--   * ``user_agent`` — first thing the device sent on registration; used
--     as a tiebreaker when the user has two devices of the same kind.

CREATE TABLE IF NOT EXISTS device (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id                   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name                      TEXT NOT NULL,
    kind                      TEXT NOT NULL DEFAULT 'other',
    device_token              TEXT NOT NULL UNIQUE,
    capture_paused            INTEGER NOT NULL DEFAULT 0,
    capture_interval_seconds  REAL,
    created_at                TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at              TEXT,
    user_agent                TEXT
);

CREATE INDEX IF NOT EXISTS idx_device_user    ON device(user_id);
CREATE INDEX IF NOT EXISTS idx_device_token   ON device(device_token);
CREATE INDEX IF NOT EXISTS idx_device_last_seen ON device(last_seen_at);
