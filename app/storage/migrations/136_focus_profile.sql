-- v1.49 — Per-app focus profiles.
--
-- A "focus profile" is a single-click bundle of capture-loop tuning
-- knobs the operator switches between depending on what they're doing
-- in the next thirty minutes:
--
--   * Deep Work     — slow 30-second cadence, audio paused, theme dark.
--   * Pair Coding   — fast 5-second cadence so the timeline stays useful
--                     while two people poke at the same IDE; audio on.
--   * Meeting       — slowest cadence (60s) + audio paused so neither
--                     the screen-grabber nor the mic interferes with the
--                     call; theme dark to keep glare off the operator's
--                     face on a webcam.
--   * Reading       — slow cadence (60s) and everything paused; the
--                     operator is reading a PDF and does not want any
--                     ambient telemetry while they think.
--
-- Each preset maps onto five existing kv_settings rows so activating a
-- profile is a no-op for everything else on the system — capture loop,
-- meeting detector and theme renderer keep reading the same kv rows
-- they always have:
--
--   capture_interval_seconds_live   — used by app.workers.capture_loop
--   capture_screens_disabled        — master screen kill switch
--   audio_capture_paused_live       — master mic kill switch
--   meeting_pause_enabled           — toggles the v1.19 smart-pause
--   theme                           — dark / light / auto
--
-- Schema notes:
--   name             — operator-facing label, UNIQUE so the install_preset
--                      helper can use INSERT OR IGNORE for idempotency.
--   description      — optional human-readable hint rendered on the card.
--   capture_interval_seconds
--                    — REAL because the kv row stores it as a stringified
--                      float; NULL means "do not touch the kv row when
--                      activating", letting the operator build hybrid
--                      profiles that only flip one knob.
--   audio_paused     — 0/1 mirror of audio_capture_paused_live.
--   blocklist_apps   — comma-separated app names; reserved for the
--                      future focus_blocklist integration. Stored as
--                      TEXT for forward-compat; nothing in app.focus_profiles
--                      reads it yet.
--   meeting_pause_enabled
--                    — 0/1 mirror of the kv row of the same name.
--   theme            — one of dark/light/auto; NULL means "do not touch".
--   is_active        — exactly one row may carry 1; the activate_profile
--                      helper enforces this in a transaction.
--   created_at       — ISO timestamp, default datetime('now'). Matches
--                      every other v1.4x table.
--
-- Indices:
--   idx_focus_profile_active — partial index over the single active row
--                              so the "which profile is on?" lookup is a
--                              constant-time index probe.

CREATE TABLE IF NOT EXISTS focus_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    capture_interval_seconds REAL,
    audio_paused INTEGER NOT NULL DEFAULT 0,
    blocklist_apps TEXT,
    meeting_pause_enabled INTEGER NOT NULL DEFAULT 1,
    theme TEXT,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_focus_profile_active
    ON focus_profile(is_active) WHERE is_active = 1;
