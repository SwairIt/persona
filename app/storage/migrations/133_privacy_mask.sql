-- v1.47 — Per-shot privacy mask regions.
--
-- The user can mark sensitive rectangles on a screenshot ("hide that
-- API token", "hide my address") so a flat, redacted PNG can be shared
-- without leaking the underlying pixels. Each row is one black-filled
-- rectangle painted over the original thumbnail when
-- ``render_masked_image`` is asked for the safe-to-share variant.
--
-- Schema notes:
--   screenshot_id  — ON DELETE CASCADE so the rectangles vanish with
--                    the parent shot. Indexed via idx_shot_privacy_mask_shot
--                    because the only read path is "give me all masks
--                    for shot X" (editor page + render endpoint).
--   x, y, width,   — pixel coordinates in the *original thumbnail*
--   height           coordinate space. The editor maps mouse drags from
--                    the rendered canvas back into thumbnail-native
--                    pixels via the natural-vs-rendered ratio so the
--                    rectangles render at the same logical location
--                    regardless of viewport size.
--   label          — optional human note ("home address", "JWT") so the
--                    operator remembers why the box is there when they
--                    revisit the editor weeks later. Free-text, no CHECK.
--   created_at     — bookkeeping only.

CREATE TABLE IF NOT EXISTS shot_privacy_mask (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    screenshot_id INTEGER NOT NULL
        REFERENCES screenshots(id) ON DELETE CASCADE,
    x INTEGER NOT NULL,
    y INTEGER NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_shot_privacy_mask_shot
    ON shot_privacy_mask(screenshot_id);
