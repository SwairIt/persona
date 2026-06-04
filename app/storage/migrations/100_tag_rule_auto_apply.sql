-- v0.100 — watermark table for the background tag-rule auto-applier.
--
-- The worker (app.workers.tag_rule_worker) walks every enabled row in
-- regex_auto_tag_rules and looks for new screenshots whose id is greater
-- than the per-rule watermark stored here. Keeping the cursor in its own
-- table means we never mutate regex_auto_tag_rules from the worker — that
-- table stays the human-managed source of truth — and a newly created
-- rule starts from id 0 so its first tick backfills history.

CREATE TABLE IF NOT EXISTS tag_rule_watermark (
    rule_id INTEGER PRIMARY KEY,
    last_screenshot_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
