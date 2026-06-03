-- v0.52 — screenshot dimensions index for size-based filtering.
--
-- The ``screenshots`` table already declares ``width INTEGER NOT NULL``
-- and ``height INTEGER NOT NULL`` in ``schema.sql`` for any DB created at
-- v0.30 or later, but Persona deployments older than v0.30 predate those
-- columns. This migration adds them defensively for those legacy DBs so
-- the dimensions backfill (:mod:`app.shot_dimensions`) and the
-- ``min_w``/``min_h`` search filter can rely on the columns existing.
--
-- SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``. The
-- migration runner (:func:`app.storage.db.init_database`) catches
-- ``duplicate column name`` errors per-statement (v0.51 split path), so
-- on any modern DB both ALTERs no-op silently and the index still
-- materialises. Keep each ALTER on its own statement so a single
-- duplicate-column error never short-circuits the other one.
--
-- Both columns are nullable here even though the schema declares them
-- NOT NULL — SQLite's ``ADD COLUMN`` cannot add a NOT NULL column to a
-- populated table without a default, and the backfill is the one that
-- populates the legacy rows. Search code MUST tolerate ``NULL`` on both
-- columns (only filter when the user supplies ``min_w`` / ``min_h``).
--
-- The partial index targets the "find tall portrait shots" / "find 4K
-- shots" use case: it stays partial (only rows with both dimensions
-- recorded) so its footprint scales with the backfilled subset, not the
-- full ``screenshots`` table.

ALTER TABLE screenshots ADD COLUMN width INTEGER;

ALTER TABLE screenshots ADD COLUMN height INTEGER;

CREATE INDEX IF NOT EXISTS idx_screenshots_dimensions
    ON screenshots(width, height) WHERE width IS NOT NULL AND height IS NOT NULL;
