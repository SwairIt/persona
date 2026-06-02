-- Tag-driven auto-collections: a saved view of every screenshot carrying a
-- given tag. Public rules are accessible from any client; private ones are
-- gated to loopback requests (Persona is a local-first app).
CREATE TABLE IF NOT EXISTS auto_collection (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    tag TEXT NOT NULL,
    public INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_auto_collection_tag ON auto_collection(tag);
