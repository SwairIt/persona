-- v1.11 fix: align audio_segment column names with Python code.
-- The original migration 092 used duration_s + started_at, but the route
-- handlers assume duration_seconds + captured_at. SQLite 3.25+ supports
-- RENAME COLUMN; the idempotent migration runner swallows duplicate errors
-- so re-running these on an already-renamed table is safe.

ALTER TABLE audio_segment RENAME COLUMN duration_s TO duration_seconds;
ALTER TABLE audio_segment RENAME COLUMN started_at TO captured_at;
