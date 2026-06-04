-- v1.13 — screenshots.idle_seconds backfill migration.
--
-- The "idle activity" feature (idle_stats.py, idle_week.py, daily/weekly
-- digests, capture_loop) reads/writes screenshots.idle_seconds, but the
-- migration that added the column was never committed. Production rows
-- silently lost their idle value and every /idle* test errored with
-- "no such column: idle_seconds".
--
-- Add the column NULL-able so historical rows stay untouched. New rows
-- get the value from app/capture/capture_loop.py via Win32
-- GetLastInputInfo (mac agent will fill it via IOKit).

ALTER TABLE screenshots ADD COLUMN idle_seconds INTEGER;

CREATE INDEX IF NOT EXISTS idx_screenshots_idle_seconds
    ON screenshots(idle_seconds)
    WHERE idle_seconds IS NOT NULL;
