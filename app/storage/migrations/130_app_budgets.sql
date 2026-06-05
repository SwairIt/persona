-- v1.45 — per-app usage budget caps.
--
-- The operator picks one or more apps and assigns each a daily-minutes
-- cap (e.g. "Twitter, 30 min"). A background worker tallies today's
-- captured minutes for each active row, compares against the cap, and
-- pushes a notification once the cap is breached. Edit / disable /
-- delete from /settings/app-budgets.
--
-- Schema notes:
--   app_name              — UNIQUE so the UPSERT path in
--                           app_budgets.upsert_budget can rely on
--                           INSERT … ON CONFLICT(app_name) DO UPDATE.
--                           Matching against screenshots.app_name is
--                           done verbatim, so the form is expected to
--                           store the same string the capture loop
--                           records (no casefold here — caller decides).
--   daily_minutes_cap     — INTEGER minutes; the worker treats it as
--                           ">= cap → breached", so 0 effectively
--                           means "alert on first sample".
--   enabled               — soft toggle so the operator can pause an
--                           alert rule without losing the cap value.
--                           The worker filters on enabled = 1.
--   alert_severity        — passed straight into notifications.push();
--                           CHECK keeps the value inside the subset the
--                           UI lets the operator pick (info, warn).
--                           error is reserved for harder system failures
--                           and is intentionally not selectable here.
--   created_at            — bookkeeping only — no index, no UI sort.

CREATE TABLE IF NOT EXISTS app_budget (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL UNIQUE,
    daily_minutes_cap INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    alert_severity TEXT NOT NULL DEFAULT 'info'
        CHECK (alert_severity IN ('info', 'warn')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_app_budget_enabled
    ON app_budget(enabled) WHERE enabled = 1;
