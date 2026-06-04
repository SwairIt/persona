-- v1.27 — auto-pin rule set + per-rule scan watermark.
--
-- A power-user pins screenshots whose OCR text matches one of these
-- regex patterns automatically. Mirrors the shape of
-- ``regex_auto_tag_rules`` + ``tag_rule_watermark`` (migration 100) so
-- the rest of the codebase — admin UI, worker, watermark logic —
-- follows the existing playbook instead of inventing a new one.
--
-- Defensive design: the worker that walks this table caps auto-pins at
-- 20 per UTC day; a bad regex can therefore pin at most 20 shots before
-- the operator notices, instead of pinning everything captured today
-- and freezing them in the ``pinned`` tier forever.
--
-- Columns (``auto_pin_rule``):
--   pattern     — raw regex source (compiled by the engine with
--                 ``re.IGNORECASE``; never mutated by storage).
--   enabled     — soft toggle so the operator can pause a rule without
--                 deleting it.
--   description — free-form note (e.g. "client name X — always keep").
--                 Nullable.
--   created_at  — ISO-8601 UTC timestamp from SQLite (``datetime('now')``).
--
-- The watermark table holds one row per rule with the largest
-- ``screenshots.id`` already inspected. Default ``0`` means a freshly
-- created rule backfills history on its first tick — same contract as
-- the tag-rule worker.

CREATE TABLE IF NOT EXISTS auto_pin_rule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    description TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_auto_pin_rule_enabled
    ON auto_pin_rule(enabled);

CREATE TABLE IF NOT EXISTS auto_pin_watermark (
    rule_id INTEGER PRIMARY KEY,
    last_screenshot_id INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
