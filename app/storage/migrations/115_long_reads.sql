-- v1.39 — auto-bookmark on long-read sessions.
--
-- Captures groups of consecutive screenshots sharing the same
-- ``window_title`` that span at least ``min_duration_minutes`` minutes
-- with no inter-shot gap longer than ``idle_threshold_seconds``. Detection
-- is performed by :func:`app.long_read_detector.detect_long_reads` and
-- triggered by ``app.workers.long_read_worker`` on a 600-second poll.
--
-- The table is INSERT-only from the detector's point of view: a
-- ``UNIQUE(started_at)`` constraint plus ``ON CONFLICT(started_at) DO
-- NOTHING`` keeps re-runs of the detector idempotent — the same long-read
-- session can be re-detected on every tick without producing duplicates,
-- because ``started_at`` is taken verbatim from the first screenshot's
-- ``captured_at`` and is therefore stable across detector runs.
--
-- ``screenshot_id_first`` / ``screenshot_id_last`` are FK references but
-- intentionally NOT ``ON DELETE CASCADE`` — retention sweeps that prune
-- the bounding shot should not destroy the long-read bookmark; the FK
-- becomes NULL via ``ON DELETE SET NULL`` so the historical record
-- survives even after the screenshot itself is gone.

CREATE TABLE IF NOT EXISTS long_read (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_title TEXT NOT NULL,
    app_name TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL,
    screenshot_id_first INTEGER REFERENCES screenshots(id) ON DELETE SET NULL,
    screenshot_id_last INTEGER REFERENCES screenshots(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(started_at)
);

CREATE INDEX IF NOT EXISTS idx_long_read_started ON long_read(started_at);
