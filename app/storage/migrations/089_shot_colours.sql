-- v1.8 feature 2/3 — per-shot dominant colour palette.
--
-- Background
-- ----------
-- Persona already stamps two narrowly-scoped colours per OCR word
-- (``bg_hex`` / ``fg_hex`` — see ``049_ocr_word_colours.sql``), but only
-- inside the bbox of recognised glyphs. The rest of the canvas — the
-- chrome, the wallpaper, the document body around the words — is
-- invisible to the search layer. That makes it impossible to answer
-- visual questions like "show me the screenshots where I was looking at
-- a mostly-orange app" or "which shots match the brand palette of the
-- design I uploaded".
--
-- This migration adds a tiny side-table that holds the dominant N
-- colours of *the entire thumbnail*, computed once via
-- :func:`PIL.Image.quantize` and cached. The companion module
-- :mod:`app.shot_colours` writes the row; the public route layer in
-- :mod:`app.web.routes.shot_colours` reads it.
--
-- Columns
-- -------
--   * ``shot_id``       — primary key, one row per screenshot.
--                         ``ON DELETE CASCADE`` so cleaning up a
--                         screenshot (recycle bin, retention sweeps,
--                         manual delete) drops the palette with it —
--                         the cache is worthless without the source
--                         image anyway.
--   * ``palette_json``  — JSON-encoded ``[{hex, weight_pct}, ...]``.
--                         Stored as TEXT (not a relational fan-out)
--                         because the palette is always read whole, never
--                         filtered or joined per-entry; JSON keeps the
--                         schema flat and makes the route's JSON
--                         endpoint a zero-copy pass-through.
--   * ``computed_at``   — UTC timestamp the row landed. Used by the
--                         backfill job to skip already-computed shots
--                         and by ops to spot stale entries (e.g. after
--                         a thumbnail rewrite). ``datetime('now')``
--                         mirrors every other side-table default in
--                         this directory.
--
-- No secondary index. The table is keyed by ``shot_id`` and the only
-- access pattern is a point lookup ``WHERE shot_id = ?`` which the
-- primary key already covers. The backfill query joins from
-- ``screenshots`` (``LEFT JOIN`` against ``shot_id``) which also uses
-- the PK.
--
-- ``IF NOT EXISTS`` keeps the migration idempotent under re-runs of
-- :func:`app.storage.db.init_database`.

CREATE TABLE IF NOT EXISTS shot_colour (
    shot_id INTEGER PRIMARY KEY,
    palette_json TEXT NOT NULL,
    computed_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(shot_id) REFERENCES screenshots(id) ON DELETE CASCADE
);
