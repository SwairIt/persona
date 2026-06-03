-- v0.53 — saved-search alert watermark column.
--
-- The alert worker (``app.workers.saved_search_alert``) polls every
-- ``saved_search`` row on a 5-minute cadence and fires a webhook event
-- ``saved_search.matched`` whenever new screenshots inserted since the
-- previous tick still satisfy the bookmarked FTS query.
--
-- ``last_checked_at`` is the per-row watermark the worker advances after
-- every successful poll. A NULL value means "never polled before" — on
-- the first tick the worker treats that as ``datetime('now')`` so a fresh
-- bookmark never floods the receiver with the entire backlog.
--
-- The column is intentionally added separately from the v0.27 baseline
-- (migration 025) so older databases pick up the alert feature without
-- rewriting any existing row. ``ALTER TABLE ... ADD COLUMN`` is wrapped
-- by ``init_database`` to swallow the "duplicate column name" error on
-- re-runs, keeping the migration idempotent.

ALTER TABLE saved_search ADD COLUMN last_checked_at TEXT;
