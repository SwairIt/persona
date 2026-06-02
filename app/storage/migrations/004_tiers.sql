-- v0.3: tiered retention.
--   hot  = full-resolution thumbnail (recent days)
--   warm = downscaled thumbnail (8-30 days)
--   cold = no thumbnail, metadata + OCR + embedding only (30+ days)
--   pinned = stays hot forever (user-marked important)

ALTER TABLE screenshots ADD COLUMN tier TEXT NOT NULL DEFAULT 'hot'
    CHECK (tier IN ('hot', 'warm', 'cold', 'pinned'));

CREATE INDEX IF NOT EXISTS idx_screenshots_tier ON screenshots(tier);

CREATE TABLE IF NOT EXISTS daily_size_log (
    day TEXT PRIMARY KEY,
    thumbnails_bytes INTEGER NOT NULL DEFAULT 0,
    screenshot_count INTEGER NOT NULL DEFAULT 0,
    sampled_at TEXT NOT NULL DEFAULT (datetime('now'))
);
