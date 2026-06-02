-- v0.35 — per-word OCR confidence overlay.
--
-- Tesseract returns a per-word ``conf`` score (0-100) alongside the bounding
-- box for every recognised token via ``pytesseract.image_to_data``. The OCR
-- worker now stores that data so the UI can render an overlay on top of the
-- thumbnail, colour-coding low-confidence words (red < 50, amber 50-79,
-- green >= 80) and letting the user click a word to launch a search.
--
-- Negative ``conf`` values (Tesseract emits ``-1`` for ignored layout rows)
-- and empty ``word`` strings are filtered by the worker before insertion —
-- we keep the schema strict (``conf INTEGER NOT NULL`` >= 0) so the table
-- stays lean.
--
-- ``ON DELETE CASCADE`` on the FK is critical: when a screenshot is purged
-- by retention / bulk-delete we never want orphan word rows lingering. The
-- index on ``screenshot_id`` keeps the overlay query (``... WHERE
-- screenshot_id = ?``) O(log n) regardless of the table's overall size.

CREATE TABLE IF NOT EXISTS ocr_word (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    screenshot_id INTEGER NOT NULL,
    word TEXT NOT NULL,
    conf INTEGER NOT NULL,
    left INTEGER,
    top INTEGER,
    width INTEGER,
    height INTEGER,
    FOREIGN KEY(screenshot_id) REFERENCES screenshots(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ocr_word_screenshot ON ocr_word(screenshot_id);
