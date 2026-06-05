-- 143 — per-app favicon chip cache (lightweight glyph + colour, not PNG).
--
-- Sibling to the v0.45 ``app_icon`` PNG-blob cache (see migration 044).
-- That table stores rasterised 64x64 PNGs extracted from running exes;
-- this one stores a tiny glyph (single emoji / letter) plus a tailwind-
-- friendly hex colour for lightweight "favicon chips" in tight lists
-- (timeline rows, autocomplete items, search suggestions) where firing
-- a PNG fetch per row would be wasteful.
--
-- Naming note: the task brief calls the table ``app_icon`` but that name
-- is already taken by the PNG cache above and the two schemas are
-- incompatible (BLOB vs glyph/colour). We use ``app_icon_chip`` so both
-- features coexist without a schema-init crash. The migration filename
-- still says ``app_icon`` so a future maintainer searching the brief
-- finds it.
--
-- ``source`` records where the row came from so a maintenance job can
-- selectively re-bake one variant:
--   * ``fallback`` — deterministic first-letter + hash-derived colour.
--   * ``bundled``  — drawn from BUNDLED_ICONS in :mod:`app.app_icon_chips`
--     when the lowercased app_name matches a known editor / browser /
--     chat client.
--   * ``user``     — operator-chosen glyph + colour via the settings UI.
--
-- ``icon_path`` is reserved for a future "upload an SVG / PNG path"
-- override path; today every row stores its glyph inline in
-- ``icon_color`` + the application-layer BUNDLED_ICONS / fallback logic
-- and ``icon_path`` stays NULL. Keeping the column lets the upload
-- feature ship without another migration.

CREATE TABLE IF NOT EXISTS app_icon_chip (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL UNIQUE,
    icon_path TEXT,
    icon_color TEXT,
    source TEXT NOT NULL DEFAULT 'fallback' CHECK (source IN ('fallback', 'bundled', 'user')),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_app_icon_chip_source ON app_icon_chip(source);
