-- v1.7 feature 2/3 — per-collection visit receipts for the public
-- ``/collection/{slug}`` viewer.
--
-- Background
-- ----------
-- The v1.3 ``share_visit`` table (migration 055) already journals every
-- successful open of a single-shot share link. Auto-collection rules
-- (the ``auto_collection`` table — see :mod:`app.web.routes.auto_collections`)
-- have no such journal: an operator can pin a slug to a tag and broadcast
-- the resulting ``/collection/{slug}`` URL, but cannot tell whether
-- anybody ever opened it, when, or from where.
--
-- This migration adds a tiny side-table that records exactly that signal
-- — one row per successful render of the public collection page.
--
-- Columns
-- -------
--   * ``slug``       — the auto-collection slug. NOT a foreign key onto
--                      ``auto_collection(slug)``: keeping the journal
--                      FK-free lets the rule be deleted (or renamed)
--                      without cascading the visit history with it, the
--                      same reasoning as the v0.55 share_visit table.
--   * ``visited_at`` — UTC timestamp the viewer rendered the page.
--   * ``ua``         — User-Agent header, truncated by the route layer
--                      to 200 chars. NULL when the header was absent or
--                      whitespace-only.
--   * ``ip_prefix``  — First two octets of the client IP (IPv4) or the
--                      first two ``:``-separated groups (IPv6). Enough
--                      to bucket "same network" patterns without ever
--                      persisting a full identifying address. NULL when
--                      the X-Forwarded-For chain produced nothing
--                      parseable. Mirrors the v0.55 share_visit shape.
--
-- Index
-- -----
-- ``idx_collection_visit_slug_visited`` is a compound index on
-- ``(slug, visited_at)`` — the stats route filters by slug *and* by a
-- recency window (``visited_at >= datetime('now', '-30 days')``), and
-- both predicates benefit from the same B-tree. A second index on
-- ``visited_at`` alone is unnecessary because the aggregator always
-- groups by slug at the same time.
--
-- ``IF NOT EXISTS`` clauses keep the migration idempotent across re-runs
-- of :func:`app.storage.db.init_database`.

CREATE TABLE IF NOT EXISTS collection_visit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL,
    visited_at TEXT NOT NULL DEFAULT (datetime('now')),
    ua TEXT,
    ip_prefix TEXT
);

CREATE INDEX IF NOT EXISTS idx_collection_visit_slug_visited
    ON collection_visit(slug, visited_at);
