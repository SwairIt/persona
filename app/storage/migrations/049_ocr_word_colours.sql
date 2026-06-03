-- v0.51 — per-word OCR colour context (background + foreground hex).
--
-- v0.35 (``035_ocr_words.sql``) introduced ``ocr_word`` with per-word
-- bounding boxes and confidence. This migration extends each row with
-- the two dominant colours sampled from inside the bbox, computed by
-- :func:`app.ocr.colour_sample.sample_colours`:
--
--   * ``bg_hex``  — the more-common pixel cluster (background fill behind
--     the glyph). For most UIs this lands on the surface colour (light
--     cards: ``#ffffff`` / dark cards: ``#0b1220`` / coloured banners:
--     whatever the brand surface is).
--   * ``fg_hex``  — the less-common cluster (the ink itself). Catches
--     things like a bright-red Slack error string against a dark chat
--     surface.
--
-- Both colours are stored as a leading-``#`` lowercase hex string
-- (``#rrggbb``); ``NULL`` means the worker hasn't yet sampled the row,
-- the sample cap (first N words per shot) excluded it, or PIL refused
-- the crop. Search code MUST tolerate both ``NULL`` and the literal
-- ``'#......'`` form. No CHECK constraint here — the colour-sample
-- helper is the single writer and it controls the format.
--
-- SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``. The
-- migration runner (:func:`app.storage.db.init_database`) re-executes
-- every ``*.sql`` file on each startup, so these ``ADD COLUMN``
-- statements would fail the second time around. We rely on the runner
-- catching ``duplicate column`` errors per-statement (v0.51 change
-- in ``app/storage/db.py``) — the file as a whole stays idempotent.
--
-- The optional index targets the "find shots with red error text" use
-- case: it stays partial (only rows that *have* a foreground colour
-- recorded), so its footprint scales with the colour-sample cap, not
-- the full ``ocr_word`` table.

ALTER TABLE ocr_word ADD COLUMN bg_hex TEXT;

ALTER TABLE ocr_word ADD COLUMN fg_hex TEXT;

CREATE INDEX IF NOT EXISTS idx_ocr_word_fg_hex
    ON ocr_word(fg_hex) WHERE fg_hex IS NOT NULL;
