-- v1.22 — Screenshot annotation revision history (autosave timeline).
--
-- The annotation editor (migration 104_screenshot_annotations.sql, route
-- ``/shot/{id}/annotate``) stores ONE live ``shot_annotation`` row per
-- screenshot and overwrites it on every save. v1.22 adds a 2-second
-- debounced *autosave* that fires while the user is still drawing; to
-- protect against a browser crash mid-session — and to give power users
-- a revert-to-earlier-state timeline — every autosave (and every
-- explicit Save click) ALSO appends an immutable revision row here.
--
-- Schema notes:
--   shot_annotation_revision.source — two-valued enum (autosave/manual)
--                                     enforced by CHECK so a typo at the
--                                     producer call site fails loudly
--                                     instead of silently landing as the
--                                     default tone.
--   shot_annotation_revision.svg_payload — the full sanitised SVG inner
--                                          markup at the moment of save;
--                                          sized identically to the live
--                                          ``shot_annotation`` payload
--                                          (≤64 KiB enforced at the
--                                          Python layer, no DB cap).
--   idx_shot_anno_rev_shot — hot path: "give me this shot's last N
--                            revisions" for the timeline UI.
--   idx_shot_anno_rev_saved — secondary index for retention sweeps that
--                             prune by age (``DELETE WHERE saved_at < ?``).
--
-- ``ON DELETE CASCADE`` cleans revisions when the parent screenshot is
-- pruned by retention sweeps; the foreign-key enforcement is enabled by
-- the runtime PRAGMA in ``app/storage/db.py``.

CREATE TABLE IF NOT EXISTS shot_annotation_revision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    screenshot_id INTEGER NOT NULL
        REFERENCES screenshots(id) ON DELETE CASCADE,
    svg_payload TEXT NOT NULL,
    saved_at TEXT NOT NULL DEFAULT (datetime('now')),
    source TEXT NOT NULL DEFAULT 'autosave'
        CHECK (source IN ('autosave', 'manual'))
);

CREATE INDEX IF NOT EXISTS idx_shot_anno_rev_shot
    ON shot_annotation_revision(screenshot_id);

CREATE INDEX IF NOT EXISTS idx_shot_anno_rev_saved
    ON shot_annotation_revision(saved_at);
