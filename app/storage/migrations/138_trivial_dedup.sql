-- v1.42 — smart-capture trivial-change suppressor.
--
-- The runtime pHash deduper (``app.dedup.find_or_create_dedup_group``)
-- already clusters near-identical shots whose Hamming distance falls
-- under a fixed threshold. Shots just outside that ceiling — a blinking
-- cursor, a clock tick, a pixel-level antialias drift — are *visually*
-- the same scene but get their own ``dedup_group_id`` because pHash
-- shifts by a single bit too many.
--
-- This migration adds a second-pass marker, ``trivial_dup_of_id``, that
-- the offline detector in :mod:`app.smart_dedup` populates by combining
-- pHash similarity with OCR-text equivalence (after stripping digits +
-- whitespace + punctuation). When set, the row is logically a trivial
-- duplicate of the earlier shot it points at; admin UIs can hide such
-- rows by adding ``WHERE trivial_dup_of_id IS NULL`` to their queries.
--
-- The reference is intentionally NOT ``ON DELETE CASCADE`` — soft- or
-- hard-deleting the "kept" anchor must not also vapourise the marker
-- on every follow-on tick; leaving the FK dangling-but-null-on-delete
-- (default SQLite behaviour: the column just keeps holding the now-
-- missing id) is what the display-filter consumers expect anyway.
-- An explicit index keeps the predicate cheap on the timeline path.

ALTER TABLE screenshots ADD COLUMN trivial_dup_of_id INTEGER REFERENCES screenshots(id);

CREATE INDEX IF NOT EXISTS idx_screenshots_trivial_dup ON screenshots(trivial_dup_of_id);
