-- v0.86 — user-defined dashboard widgets.
--
-- Background
-- ----------
-- The existing dashboard (v0.65) shows a fixed set of cards; v0.81 let
-- the user reorder/hide them but kept the *set* closed. v0.86 opens
-- that set up: a "widget" is a saved FTS5 search query that renders on
-- /dashboard as a tile showing the live match count. The shape mirrors
-- a stripped-down ``saved_search`` (025) — title + query — minus the
-- slug column, because widgets are positional rather than addressable:
-- they're never opened by URL, only rendered in-place. ``id`` does
-- double duty as primary key + delete handle for the editor's POST.
--
-- Columns
-- -------
--   * ``title``      — human-readable label rendered on the tile.
--   * ``query``      — FTS5 query string passed to ``app.search.search``
--                      verbatim; the search helper already sanitises
--                      against MATCH-parser breakage so we don't need
--                      additional shape rules in the schema.
--   * ``position``   — ascending integer ordering tiles render in.
--                      Writers append ``MAX(position)+1`` (the route
--                      layer enforces this) so deletes leave gaps but
--                      never collide.
--   * ``created_at`` — wall-clock UTC ISO string for the editor's
--                      "added" footer; ``datetime('now')`` matches the
--                      pattern used by ``feed_token`` (074) and
--                      ``saved_search`` (025) for cross-migration
--                      consistency.
--
-- ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` keep
-- the migration idempotent across re-runs of ``init_database``.

CREATE TABLE IF NOT EXISTS dashboard_widget (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    query TEXT NOT NULL,
    position INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_dashboard_widget_position
    ON dashboard_widget(position);
