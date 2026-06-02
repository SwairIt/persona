-- v0.44 — per-webhook event type filters.
--
-- Until now each webhook row carried a single ``event_type`` (migration
-- 006) and the dispatcher fired only when that exact string matched the
-- current event. v0.44 introduces a multi-event subscription column,
-- ``event_types``: a comma-separated list of event types — or the
-- literal asterisk ``*`` meaning "fire on every event".
--
-- Matching is done in :func:`app.webhook_filters.should_fire`:
--   * NULL / empty / "*"   → match everything (backwards-compatible default)
--   * "a, b.c, b.*"        → match a *exactly* OR b.c *exactly* OR any
--                            event whose name starts with "b." (glob prefix)
--
-- Backfill rule: existing rows had a single legacy ``event_type`` so we
-- seed ``event_types`` with that value. New rows can either pick an
-- explicit list or leave ``*`` to receive everything. SQLite has no
-- ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS`` so this migration is
-- guarded by the migration runner's "remember which files ran" table —
-- it will only ever execute once per database.

ALTER TABLE webhooks ADD COLUMN event_types TEXT DEFAULT '*';

UPDATE webhooks
   SET event_types = event_type
 WHERE event_types IS NULL OR event_types = '*';
