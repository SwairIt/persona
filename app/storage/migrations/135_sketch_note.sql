-- v1.48 — Freehand sketch notes.
--
-- A "sketch note" is a quick stylus/mouse doodle the user captures in
-- the browser at /sketch — an arrow on a wireframe, a tree of ideas,
-- a graph sketch — and stores alongside the regular text notes so it
-- becomes searchable in the same memory surface. We persist the raw
-- SVG payload rather than rasterising to PNG so:
--
--   * the markup is replayable at any resolution without aliasing;
--   * the on-disk footprint is tiny (paths beat pixels for line art);
--   * the renderer is just the browser — no Pillow / cairo round-trip
--     and no server-side image worker to babysit.
--
-- Schema notes:
--   svg_payload — the full ``<svg ...>...</svg>`` document the editor
--                 produced, already passed through ``sanitize_svg`` in
--                 :mod:`app.sketch_notes` (no ``<script>``, no ``on*``
--                 handlers, no ``javascript:`` URIs). Stored as TEXT;
--                 SQLite has no separate XML type.
--   width,      — viewport dimensions in CSS pixels at draw time. We
--   height        keep them on the row so a thumbnail card can reserve
--                 the right aspect-ratio box without parsing the SVG.
--   created_at — ISO timestamp, default ``datetime('now')``. Same
--                bookkeeping shape every other v1.4x table uses.
--   tags       — comma-separated tag list (free-text, optional).
--                Mirrors the cheap tag column on ``notes`` rather than
--                a separate join table — sketches are a low-volume
--                surface and the existing tag-faceting code already
--                copes with the comma form.
--
-- Indices:
--   idx_sketch_note_created — supports the "newest first" sort on
--                              /sketches and the list JSON endpoint.

CREATE TABLE IF NOT EXISTS sketch_note (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    svg_payload TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    tags TEXT
);

CREATE INDEX IF NOT EXISTS idx_sketch_note_created
    ON sketch_note(created_at);
