-- v0.57 per-app day digest cache.
-- One-sentence LLM summary of what the user did in a single app on a single
-- calendar day. Surfaces on /digest/apps?day=YYYY-MM-DD as a table of
-- (app_name, tldr, regenerate). Generated lazily on demand and cached here
-- so a page reload does not re-spend LLM tokens.
--
-- Sibling to the per-day TL;DR table (`day_tldr`, migration 038) but keyed
-- by (day, app_name) instead of just day. We keep this in its own table
-- rather than widening `day_tldr` so the simpler whole-day cache stays
-- cheap to query and the two features can evolve independently.

CREATE TABLE IF NOT EXISTS app_day_digest (
    day TEXT NOT NULL,              -- YYYY-MM-DD
    app_name TEXT NOT NULL,
    tldr TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (day, app_name)
);

CREATE INDEX IF NOT EXISTS idx_app_day_digest_day ON app_day_digest(day);
