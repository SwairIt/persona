-- v1.46 — AI-suggested daily reminders.
--
-- An LLM scans the previous 24h of activity (hourly_card + daily_pin +
-- notes) and surfaces up to 3 things worth remembering tomorrow. Each
-- suggestion lands as one row here; the user dismisses or snoozes them
-- from /reminders/ai. Generation is driven by ai_reminders_worker,
-- which fires once per local day at ai_reminders_hour_local (default
-- 22:00 — late enough that the day's signal is settled, early enough
-- that the user can glance at the list before bed).
--
-- Schema notes:
--   source_day      — calendar day (YYYY-MM-DD, local TZ) whose signal
--                     drove the suggestion. Plain text so the rendering
--                     route can group by day cheaply.
--   severity        — UI tone: info (default) / warn / action. CHECK
--                     keeps a stray write from poisoning the template,
--                     and DEFAULT lets the LLM omit the field entirely.
--   dismissed_at    — soft delete. NULL = still in the bell-style list;
--                     non-NULL hides the row from /reminders/ai but
--                     leaves it queryable for the analytics layer.
--   due_at          — optional ISO timestamp the LLM may attach when the
--                     suggestion is time-bound ("call the bank before
--                     17:00"). NULL means "whenever convenient
--                     tomorrow".
--
-- Indices:
--   idx_ai_reminder_due           — supports the "what's due soon" sort
--                                   on /reminders/ai.
--   idx_ai_reminder_undismissed   — partial index keeps the hot path
--                                   (list undismissed) cheap even after
--                                   the table grows past tens of
--                                   thousands of rows.

CREATE TABLE IF NOT EXISTS ai_reminder (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    source_day TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    severity TEXT NOT NULL DEFAULT 'info'
        CHECK (severity IN ('info', 'warn', 'action')),
    dismissed_at TEXT,
    due_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_ai_reminder_due
    ON ai_reminder(due_at);

CREATE INDEX IF NOT EXISTS idx_ai_reminder_undismissed
    ON ai_reminder(dismissed_at) WHERE dismissed_at IS NULL;
