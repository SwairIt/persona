-- v0.85 — Focus-session distraction blocker.
--
-- During a v0.36 ``focus_session`` the user wants the capture loop to
-- ignore distraction apps — Slack, Telegram, the news tab — so the
-- timeline for that session is clean and the post-mortem doesn't show
-- the very thing the session was meant to avoid. This table is the
-- allow/deny knob: any ``app_name`` listed here is skipped by the
-- capture loop, but **only** while a focus_session row exists with
-- ``ended_at IS NULL``. Outside of an active session the list has no
-- effect, which is the whole point — these apps are fine to capture
-- normally, the user just doesn't want them recorded during deep work.
--
-- Storage shape mirrors ``app_capture_skip`` (v0.67) and
-- ``ocr_skip_app`` so the operator only learns one normalisation rule:
-- the row key is the raw ``app_name`` value lowercased and stripped,
-- and the lookup is a single indexed-PK probe per capture iteration.
-- Round-tripping the normalised form back to the UI is good enough for
-- a settings page whose job is to bulk-mute apps.

CREATE TABLE IF NOT EXISTS focus_blocklist (
    app_name TEXT PRIMARY KEY,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);
