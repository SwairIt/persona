-- v1.19 — Smart-pause meeting detector log.
--
-- The capture loop watches the active and recent app names; when one of
-- them matches a known video-conference pattern (Zoom, Teams, Meet,
-- Discord, etc.) AND smart-pause is enabled, the loop skips the
-- iteration so we never silently record the meeting screen.
--
-- We record one row per *transition* — not per tick. ``started_at`` is
-- set when the loop notices we entered a meeting; ``ended_at`` is
-- updated when the meeting ends. The detector keeps the schema flat on
-- purpose: a single row per meeting makes ``GET /api/meeting-pause``
-- (which only needs the most recent event) a trivial
-- ``ORDER BY started_at DESC LIMIT 1`` lookup.
--
-- ``app_name`` is the human-readable display name we matched against
-- (e.g. ``zoom.us``, ``Microsoft Teams``); ``pattern`` is the literal
-- substring from the detector's hard-coded list (``zoom``, ``teams``).
-- Storing both lets the UI render "you were in zoom.us" while the
-- machine path still has the canonical pattern key to group by.
--
-- The descending index on ``started_at`` mirrors how the API reads —
-- newest first, single row. ``IF NOT EXISTS`` keeps the migration
-- idempotent across re-runs.

CREATE TABLE IF NOT EXISTS meeting_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL,
    pattern TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_meeting_event_started_at_desc
    ON meeting_event(started_at DESC);
