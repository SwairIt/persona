-- v0.30 — HMAC-SHA256 signing for outbound webhooks.
--
-- The `webhooks` table from migration 006 already has a nullable `secret TEXT`
-- column. The signing layer now treats every webhook as if it MUST have a
-- secret (empty string is the "not yet generated" sentinel; the dispatcher
-- backfills via `secrets.token_urlsafe(32)` on first POST through
-- `app.webhook_signing.ensure_secret`).
--
-- SQLite has no "ALTER TABLE … ADD COLUMN IF NOT EXISTS"; the column was
-- introduced in 006 so we cannot re-add it. Instead we just normalise
-- existing NULL rows to '' so that the application's "empty-string ==
-- needs-backfill" invariant holds across upgrades.

UPDATE webhooks SET secret = '' WHERE secret IS NULL;
