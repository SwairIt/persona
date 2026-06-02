-- v0.36 — Pomodoro focus sessions.
--
-- Stand-alone ``focus_session`` table (singular) that backs the v0.36
-- Pomodoro-style timer page. It is intentionally separate from the
-- legacy ``focus_sessions`` table (plural, created in 008) — the older
-- table stores a single ``duration_minutes`` + ``intent`` per row,
-- while the new timer needs to track the work / break split and a
-- free-form label so users can review what they spent each block on.
--
-- ``started_at`` is the UTC ISO 8601 timestamp the session began;
-- ``ended_at`` stays ``NULL`` while the session is still open, which is
-- how :func:`app.focus.current_session` finds the active one. ``completed``
-- is a 0/1 flag distinguishing a finished-on-time session from a bail-out
-- so the UI can colour the recent-list entries accordingly.
--
-- The descending index on ``started_at`` makes the "current session" and
-- "recent sessions" lookups O(log n) — both queries order by that column
-- and either pull the head row or a small window of the most recent rows.

CREATE TABLE IF NOT EXISTS focus_session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    work_minutes INTEGER NOT NULL,
    break_minutes INTEGER NOT NULL,
    label TEXT,
    completed INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_focus_session_started_desc
    ON focus_session(started_at DESC);
