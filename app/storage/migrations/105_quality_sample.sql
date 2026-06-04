-- v0 — Capture-quality A/B sample log.
--
-- The user routinely tunes ``thumbnail_quality`` (default 35) and
-- ``thumbnail_max_width`` (default 640) to stay inside the 25 MB/day
-- on-disk budget. Lowering either knob shrinks the WebP payload but at
-- some point starts to bite into OCR readability. This table is the
-- ledger for a small, opt-in offline study: we resample a handful of
-- recent screenshots, recompute three readability proxies (Laplacian-
-- variance sharpness, OCR character count, pHash bit-entropy) plus the
-- on-disk file size, and store them keyed by the *quality / width*
-- combination that produced the thumbnail. A later GROUP BY then
-- answers "at q=35/640px your average sharpness is X, at q=45/900px it
-- was Y last week" without ever needing to re-run capture.
--
-- ``UNIQUE(screenshot_id)`` keeps the relationship 1:1 — one sample row
-- per source shot. The sampler uses ``INSERT OR IGNORE`` so a re-run is
-- cheap and idempotent: rows already collected stay untouched and only
-- shots not yet sampled get a new row. If a future iteration wants
-- multiple measurements per shot (e.g. across re-encodes at different
-- quality bands) we drop the unique constraint and add a
-- ``sample_kind`` column instead.
--
-- ``ON DELETE CASCADE`` cleans the row when the parent screenshot is
-- pruned by retention sweeps; the foreign-key enforcement is enabled
-- by the runtime PRAGMA in ``app/storage/db.py``.
--
-- ``sharpness``, ``ocr_chars``, ``file_size_bytes`` and
-- ``phash_entropy_bits`` are all nullable so a degraded sampler
-- environment (numpy missing → no Laplacian variance, file pruned
-- mid-sweep → no stat) still records the row and the aggregation
-- contracts handle NULLs via ``AVG()``'s natural skip-NULL behaviour.

CREATE TABLE IF NOT EXISTS quality_sample (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    screenshot_id INTEGER NOT NULL
        REFERENCES screenshots(id) ON DELETE CASCADE,
    sampled_at TEXT NOT NULL DEFAULT (datetime('now')),
    quality_used INTEGER NOT NULL,
    width_used INTEGER NOT NULL,
    sharpness REAL,
    ocr_chars INTEGER,
    file_size_bytes INTEGER,
    phash_entropy_bits REAL,
    UNIQUE(screenshot_id)
);

CREATE INDEX IF NOT EXISTS idx_quality_sample_sampled_at
    ON quality_sample(sampled_at);
CREATE INDEX IF NOT EXISTS idx_quality_sample_band
    ON quality_sample(quality_used, width_used);
