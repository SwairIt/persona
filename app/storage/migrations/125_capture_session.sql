-- v1.42 — Capture session diary.
--
-- A "capture session" is a contiguous block of screenshots taken with
-- no gap larger than ``gap_threshold_seconds`` (default 30 min) between
-- adjacent shots. The :mod:`app.capture_sessions` worker walks the
-- ``screenshots`` table in time order, partitions the rows into
-- sessions, and persists one row here per session. The UI at
-- ``/sessions`` renders the resulting journal-style log of work blocks
-- — when you sat down, when you got up, what app dominated, how many
-- frames were captured and how much voice was transcribed in that
-- window.
--
-- Column contract
-- ---------------
--   * ``started_at`` / ``ended_at`` — ISO UTC stamps of the first /
--     last screenshot in the block. ``UNIQUE(started_at)`` is the
--     idempotency key: the worker re-runs on every poll cycle and we
--     rely on ``ON CONFLICT(started_at) DO NOTHING`` to avoid
--     duplicates as the same block grows.
--   * ``duration_seconds`` — wall-clock span between first and last
--     shot (NOT a sum of inter-shot intervals). Useful as the headline
--     "you worked N minutes" number; trivially derivable from the two
--     stamps but stored to keep the list query a single SELECT.
--   * ``dominant_app`` — the mode of ``screenshots.app_name`` over the
--     window. NULL if every shot in the session has a NULL app_name
--     (cold-boot screenshots before active-window probing kicks in).
--   * ``screen_count`` — number of rows from ``screenshots`` that fell
--     into this block.
--   * ``voice_seconds`` — sum of ``audio_segment.duration_seconds``
--     whose ``captured_at`` falls inside [started_at, ended_at]. May
--     legitimately be 0 — many sessions have no mic capture.
--   * ``top_titles_json`` — JSON array (max 5) of the most frequent
--     ``window_title`` strings observed, newest-tied breaks on count
--     descending. Stored as JSON so the renderer can iterate without
--     re-parsing a delimiter-separated blob.
--
-- The single index on ``started_at`` covers the only read pattern the
-- UI has — "newest sessions first" — without paying for a covering
-- index over every column.

CREATE TABLE IF NOT EXISTS capture_session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    dominant_app TEXT,
    screen_count INTEGER NOT NULL DEFAULT 0,
    voice_seconds INTEGER NOT NULL DEFAULT 0,
    top_titles_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(started_at)
);

CREATE INDEX IF NOT EXISTS idx_capture_session_started
    ON capture_session(started_at);
