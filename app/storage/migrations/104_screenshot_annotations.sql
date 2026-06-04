-- v1.20 — Per-screenshot visual annotations (SVG overlay).
--
-- Stores a single SVG payload per ``screenshots.id`` containing the
-- user-drawn rectangles, arrows and text labels rendered on top of the
-- thumbnail. The payload is the inner-SVG markup (one ``<g>`` per
-- shape) — not a full document — so the template wraps it in its own
-- ``<svg viewBox="...">`` matching the source image's natural pixels.
--
-- ``UNIQUE(screenshot_id)`` keeps the relationship 1:1 for now and lets
-- ``INSERT ... ON CONFLICT DO UPDATE`` act as a true upsert. If we ever
-- need multiple named overlays per shot we drop the unique constraint
-- and add a ``name`` column instead.
--
-- ``ON DELETE CASCADE`` cleans the row when the parent screenshot is
-- pruned by retention sweeps; the foreign-key enforcement is enabled
-- by the runtime PRAGMA in ``app/storage/db.py``.

CREATE TABLE IF NOT EXISTS shot_annotation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    screenshot_id INTEGER NOT NULL
        REFERENCES screenshots(id) ON DELETE CASCADE,
    svg_payload TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(screenshot_id)
);

CREATE INDEX IF NOT EXISTS idx_shot_annotation_screenshot_id
    ON shot_annotation(screenshot_id);
