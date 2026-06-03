-- v0.54 — per-app filter for daily/weekly LLM digests.
--
-- The daily and weekly summarisers feed the LLM every capture in the
-- target window. Some apps are noisy (password managers, system
-- monitors, full-screen video players) and dilute the signal — and the
-- user may simply not want them in the digest. This migration seeds a
-- single ``digest_app_blocklist`` row in ``kv_settings`` so the
-- summarisers can read a CSV list of app names to skip.
--
-- Format: comma-separated ``app_name`` values, matched case-insensitively
-- against ``screenshot.app_name`` during digest assembly. Empty (the
-- default) means "include every app", preserving prior behaviour for
-- existing installs.
--
-- ``INSERT OR IGNORE`` only seeds the row when it does not yet exist,
-- so re-running this migration against a database where the user has
-- already chosen a blocklist will never clobber their selection.

INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('digest_app_blocklist', '');
