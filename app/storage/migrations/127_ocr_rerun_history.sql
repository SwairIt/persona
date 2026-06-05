-- v1.46 — Per-shot OCR re-run revision log.
--
-- Migration 122 added ``screenshots.ocr_rerun_count`` and the
-- ``POST /api/screenshot/{id}/ocr-rerun`` route lets the operator
-- re-process a single shot through the OCR pipeline on demand. Until
-- this migration the *previous* ``ocr_text`` value was overwritten
-- with no audit trail — there was no way to see what the OCR layer
-- produced last time versus what it produces now.
--
-- This migration introduces ``ocr_rerun_history``: an append-only
-- snapshot table that records one row per OCR "revision" of a
-- screenshot. The :mod:`app.ocr_rerun` module writes two rows on each
-- re-run — the prior text (``initial`` on the very first re-run,
-- ``rerun`` thereafter) and the freshly extracted text (always
-- ``rerun``). The diff viewer at ``/screenshot/{id}/ocr-history``
-- compares any two revisions with :func:`difflib.unified_diff`.
--
-- Distinct from migration 076's ``ocr_history`` (which snapshots
-- *find-and-replace* / *vision-replace* edits with a ``prev_text`` /
-- ``reason`` shape): that table is keyed to bulk text edits and
-- supports a *revert* button. ``ocr_rerun_history`` is keyed to
-- automatic OCR re-extractions and supports a *diff* viewer — different
-- write paths, different consumers, intentionally separate tables.
--
-- Column contract
-- ---------------
--   * ``screenshot_id`` — FK into ``screenshots(id)``. ``ON DELETE
--     CASCADE`` so deleting a screenshot cleans up its revision log
--     without an orphan-sweep cron.
--   * ``ocr_text`` — full snapshot of the OCR text at this revision.
--     Nullable to mirror ``screenshots.ocr_text`` (an OCR pass that
--     yielded zero characters writes ``""``, not ``NULL``, by
--     convention, but the column accepts ``NULL`` to be safe).
--   * ``char_count`` — denormalised ``length(ocr_text)`` so the list
--     view does not have to ``LENGTH()`` every blob to render a table.
--     Always non-negative; ``0`` when ``ocr_text`` is NULL/empty.
--   * ``run_at`` — UTC ISO stamp of when the revision was recorded.
--     Defaults to ``datetime('now')`` so the helper does not need to
--     pass it on every insert.
--   * ``run_source`` — provenance tag: ``initial`` (the very first
--     snapshot capturing the *pre-re-run* text on a never-rerun shot),
--     ``rerun`` (any snapshot produced by the manual re-run path), or
--     ``manual`` (reserved for a future per-shot manual editor). The
--     CHECK constraint guards future writers against typos.
--
-- The two indexes cover the only read patterns the UI has:
--   * by ``screenshot_id`` for the per-shot list view, and
--   * by ``run_at`` for any future "recent revisions across all shots"
--     dashboard widget.

CREATE TABLE IF NOT EXISTS ocr_rerun_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    screenshot_id INTEGER NOT NULL REFERENCES screenshots(id) ON DELETE CASCADE,
    ocr_text TEXT,
    char_count INTEGER NOT NULL DEFAULT 0,
    run_at TEXT NOT NULL DEFAULT (datetime('now')),
    run_source TEXT NOT NULL DEFAULT 'rerun'
        CHECK (run_source IN ('initial', 'rerun', 'manual'))
);

CREATE INDEX IF NOT EXISTS idx_ocr_rerun_history_shot
    ON ocr_rerun_history(screenshot_id);

CREATE INDEX IF NOT EXISTS idx_ocr_rerun_history_run_at
    ON ocr_rerun_history(run_at);
