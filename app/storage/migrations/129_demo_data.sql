-- v1.47 — Demo-data sentinel column on ``screenshots``.
--
-- ``app/demo_seeder.py`` populates a fresh install with ~200 plausible
-- screenshot rows + 30 ``screenshot_notes`` + a handful of
-- ``hourly_card`` / ``daily_pin`` rows so the timeline, gallery, hourly
-- cards page, and notes search are non-empty when the operator is
-- recording a screencast or capturing screenshots for the website.
-- Without these rows the UI is depressingly blank on a first install,
-- which makes demos and the public README impossible.
--
-- The seeder MUST be able to undo itself idempotently — leaving a few
-- hundred fake screenshots cluttering a real database after the demo
-- is a foot-gun. We discriminate demo rows with a dedicated boolean
-- column rather than a string sentinel inside ``ocr_text`` because:
--
--   * a column survives a future "rewrite OCR" pass that would
--     otherwise clobber an ``ocr_text`` prefix marker,
--   * a column is queryable in O(rows where flag = 1) via the partial
--     index below, instead of a full FTS scan for the sentinel,
--   * the timeline and search routes can opt to hide demo rows with a
--     single ``WHERE is_demo = 0`` clause without parsing strings.
--
-- The partial index keeps the index disk footprint at zero on real
-- installs (no demo rows → no index entries) while still giving the
-- purge route an O(seeded-rows) DELETE plan.

ALTER TABLE screenshots ADD COLUMN is_demo INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_screenshots_is_demo
    ON screenshots(is_demo) WHERE is_demo = 1;
