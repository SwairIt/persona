-- v1.27 — cross-day fact extraction: persistent entity ledger.
--
-- Power users want a queryable list of "people I mentioned this month"
-- and "projects I worked on" that survives every retention sweep and
-- powers cross-day RAG ("what did I work on with Denis last month?").
--
-- The entity_extractor_worker scans hourly_card.summary + transcript
-- on a 30-min cadence, runs a heuristic capitalised-token pass (LLM
-- refinement is opt-in later), and upserts hits here. mention_count
-- accumulates across days so the top-N table is a permanent view of
-- who/what is recurring in the user's day.
--
-- Schema is intentionally minimal: one entity row per (name, kind),
-- one mention row per (entity_id, source_kind, source_id, mentioned_at)
-- — the latter is the audit trail / timeline source.

CREATE TABLE IF NOT EXISTS entity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('person', 'project', 'topic', 'other')),
    first_seen TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen TEXT NOT NULL DEFAULT (datetime('now')),
    mention_count INTEGER NOT NULL DEFAULT 1,
    UNIQUE(name, kind)
);

CREATE TABLE IF NOT EXISTS entity_mention (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER NOT NULL REFERENCES entity(id) ON DELETE CASCADE,
    source_kind TEXT NOT NULL,
    source_id INTEGER,
    mentioned_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_entity_mention_entity_time
    ON entity_mention(entity_id, mentioned_at);

CREATE INDEX IF NOT EXISTS idx_entity_last_seen
    ON entity(last_seen);
