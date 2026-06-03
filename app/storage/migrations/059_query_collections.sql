-- Saved-query collections — bundle several saved searches under one slug.
--
-- A collection has a public read-only page that re-runs every member
-- query and shows its current result count. Distinct from `saved_search`
-- (025) which stores individual bookmarks: one bookmark can belong to
-- many collections and ordering inside a collection is explicit.
CREATE TABLE IF NOT EXISTS query_collection (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    blurb TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS query_collection_member (
    collection_slug TEXT NOT NULL,
    saved_search_slug TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (collection_slug, saved_search_slug)
);

CREATE INDEX IF NOT EXISTS idx_query_collection_created_at
    ON query_collection (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_query_collection_member_position
    ON query_collection_member (collection_slug, position);
