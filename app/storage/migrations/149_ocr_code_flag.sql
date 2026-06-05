-- v1.63 — Heuristic "this OCR text looks like source code" flag on screenshots.
--
-- Background
-- ----------
-- The OCR worker (:mod:`app.workers.ocr_worker`) writes recognised text
-- to ``screenshots.ocr_text`` for every captured shot. A surprising
-- fraction of those texts are *source code* — IDE windows, terminal
-- pasted snippets, code-review diffs in a browser. Today they are
-- indistinguishable from any other OCR text on the timeline and the
-- generic search/dashboard widgets, even though "the shot where I had
-- that buggy function open" is a high-intent retrieval query.
--
-- :mod:`app.ocr_code_detector` runs a cheap, dependency-free
-- heuristic over each shot's OCR text and writes a single bit here
-- saying "this looks like code". A dedicated ``/code-shots`` browse
-- page then surfaces those shots in a thumbnail grid with a short OCR
-- preview — a focused retrieval entry-point that does not exist on any
-- of the generic timeline pages.
--
-- Schema
-- ------
--   * ``ocr_looks_like_code`` — ``INTEGER NOT NULL DEFAULT 0``.
--     Two-valued by convention (``0`` = not code, ``1`` = code) but
--     persisted as ``INTEGER`` for SQLite-affinity reasons; the column
--     is set from a Python ``bool`` so the runtime values are always
--     exactly ``0`` or ``1``. ``NOT NULL DEFAULT 0`` so existing rows
--     are treated as "not yet known to be code" — the background
--     classifier (:mod:`app.workers.ocr_code_detector_worker`) will
--     re-evaluate them on its next sweep.
--
-- Indexes
-- -------
-- A partial index on ``ocr_looks_like_code = 1`` covers the only hot
-- read path: ``/code-shots`` lists the flagged shots, newest first. A
-- partial index keeps the storage footprint tiny on a corpus where the
-- vast majority of shots are *not* code — the index entries are only
-- written for the small flagged subset.
--
-- Tolerant duplicate
-- ------------------
-- SQLite has no ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``. The
-- migration runner (:func:`app.storage.db.init_database`) swallows the
-- ``duplicate column name`` error per-statement, so re-running this
-- migration on an already-upgraded DB silently no-ops while a fresh
-- install picks the column up cleanly.

ALTER TABLE screenshots ADD COLUMN ocr_looks_like_code INTEGER NOT NULL DEFAULT 0;

CREATE INDEX idx_screenshots_code
    ON screenshots(ocr_looks_like_code)
    WHERE ocr_looks_like_code = 1;
