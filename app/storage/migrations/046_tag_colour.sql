-- v0.46 — per-tag colour customisation.
--
-- Goal: let users assign a CSS hex colour (e.g. ``#ec4899``) to each
-- tag so the chip background follows the tag everywhere (tags index,
-- screenshot detail, tag-detail, tag-trend, screenshot card, public
-- day view).
--
-- The ``color`` column was originally introduced in
-- :file:`001_tags.sql` as a nullable ``TEXT`` field, so the v0.46
-- work is purely UI + a dedicated API surface — the schema already
-- holds the data.
--
-- SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``, and the
-- migration runner (:func:`app.storage.db.init_database`) re-executes
-- every ``*.sql`` on each startup, so we cannot safely re-issue the
-- ``ADD COLUMN``. Instead this migration:
--
--   1. Normalises any stray ``''`` (empty string) colours to ``NULL``
--      so the Jinja ``tag.color or '#8b5cf6'`` fallback consistently
--      kicks in for "no colour chosen".
--   2. Adds a partial index on ``tags(color)`` to make "tags grouped
--      by colour" reports (palette, search facets) cheap. Partial
--      keeps the index tiny — only tags that actually carry a
--      colour.
--
-- Both statements are idempotent: ``UPDATE`` re-runs are harmless and
-- ``CREATE INDEX IF NOT EXISTS`` is a no-op when the index already
-- exists.

UPDATE tags SET color = NULL WHERE color = '';

CREATE INDEX IF NOT EXISTS idx_tags_color ON tags(color) WHERE color IS NOT NULL;
