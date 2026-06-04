-- v1.17 — Mic recording schedule.
--
-- The audio worker checks this row before every capture iteration:
--   - if enabled=0 → recording allowed any time the worker runs
--     (back-compat: behaves as before the schedule existed).
--   - if enabled=1 → recording allowed only when current local
--     weekday + hour fall inside the configured window.
--
-- We use kv_settings for the actual values (so the existing
-- get_kv/set_kv plumbing covers UI reads/writes). This migration is a
-- placeholder for future schema additions; the runtime contract is
-- documented at app/audio/mic_schedule.py.

-- Mic schedule fields (stored in kv_settings):
--   mic_schedule_enabled      : "0" | "1"
--   mic_schedule_days         : csv of weekday codes, e.g. "mon,tue,wed,thu,fri"
--                               (default: "mon,tue,wed,thu,fri,sat,sun")
--   mic_schedule_start_hour   : "0".."23" (local-time start, inclusive)
--   mic_schedule_end_hour     : "0".."23" (local-time end, exclusive)
--   capture_screens_disabled  : "0" | "1" (master kill-switch for screens)

INSERT OR IGNORE INTO kv_settings (key, value) VALUES
    ('mic_schedule_enabled', '0'),
    ('mic_schedule_days', 'mon,tue,wed,thu,fri,sat,sun'),
    ('mic_schedule_start_hour', '0'),
    ('mic_schedule_end_hour', '24'),
    ('capture_screens_disabled', '0');
