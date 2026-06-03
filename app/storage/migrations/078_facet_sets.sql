-- v0.95 feature 3/3 — saved facet sets (bundled search-filter combinations).
--
-- Background
-- ----------
-- The /search page accumulates a wide pile of post-filter parameters:
-- free-text ``q``, an ``app`` name, an open or closed date range
-- (``date_from`` / ``date_to`` or the legacy ``since`` / ``until``),
-- one or more ``tag`` values, a ``tier`` band, an ``ocr_length`` minimum
-- size, sort key and search ``mode``. Power users land on a useful
-- combination once and then have no way to recall it short of typing
-- the whole query string by hand.
--
-- ``saved_search`` (025) already pins a single FTS ``q`` string under a
-- slug. That is deliberately narrow: it does not carry facet state so
-- two bookmarks that look identical in the list can produce wildly
-- different result counts depending on whatever tag/app/date sliders the
-- user happens to have selected when they click "Run". A facet set
-- captures the *whole* params dict — every filter the search route
-- accepts — so re-running it returns exactly what the operator saw at
-- save time.
--
-- Columns
-- -------
--   * ``slug``        — primary key, validated by the route against
--                       ``^[a-z0-9-]{1,40}$`` so it fits safely in a
--                       URL path segment without percent-encoding.
--   * ``title``       — operator-visible label (1..120 chars). The
--                       route enforces the length; the column is just
--                       ``NOT NULL`` so a corrupt direct INSERT cannot
--                       leave the list page with an empty heading.
--   * ``params_json`` — opaque JSON blob: a flat object mapping the
--                       search route's query-string keys to either a
--                       single string (``"app"``, ``"date_from"``…) or
--                       a list of strings (``"tag"`` is the only
--                       repeatable param today). Stored as TEXT so we
--                       stay portable across SQLite versions without
--                       JSON1 — the route json.loads/dumps at the edge.
--   * ``created_at``  — ISO-8601 wall-clock from ``datetime('now')``,
--                       matching every adjacent table (025, 074, 076).
--
-- ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` keep
-- the migration idempotent across re-runs of ``init_database``.

CREATE TABLE IF NOT EXISTS facet_set (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    params_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_facet_set_created_at
    ON facet_set (created_at DESC);
