-- v0.64 — per-shot sticky-note overlays.
--
-- A "sticky note" is a free-form text scribble pinned to a single
-- (x_pct, y_pct) coordinate ON TOP of a screenshot. It is deliberately
-- distinct from the three sibling concepts already on the page:
--
--   * screenshot_notes   (one global markdown note per shot; FTS-indexed);
--   * screenshot_annotation (append-only commentary list under the shot);
--   * tag attachments    (controlled vocabulary).
--
-- Sticky notes live IN the image area, not the sidebar, so the schema
-- carries the anchor point as fractional coordinates in [0..1] rather than
-- absolute pixels — that way the same note keeps its visual position when
-- the wrapper is resized to a thumbnail, a lightbox, or a print export.
--
-- ``color`` is a free-form short string (``yellow``, ``pink``, ``blue``,
-- ``#ff00aa``). The default is ``yellow`` because that is the canonical
-- Post-it shade and the only one most users ever set. We do NOT enforce a
-- palette in SQL — the JS layer is the canonical owner of the swatch set,
-- and a constrained column would force a migration every time the palette
-- changes.
--
-- ON DELETE CASCADE so deleting a screenshot wipes its stickies and we
-- never end up with dangling overlays pointing at a void.

CREATE TABLE IF NOT EXISTS sticky_note (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shot_id INTEGER NOT NULL,
    x_pct REAL NOT NULL,
    y_pct REAL NOT NULL,
    body TEXT NOT NULL,
    color TEXT NOT NULL DEFAULT 'yellow',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(shot_id) REFERENCES screenshots(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sticky_note_shot_id
    ON sticky_note(shot_id);
