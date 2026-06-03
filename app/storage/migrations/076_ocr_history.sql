-- v0.92 feature 3/3 — OCR edit history snapshots.
--
-- Background
-- ----------
-- Three independent code paths overwrite ``screenshots.ocr_text``:
-- bulk regex find-and-replace (v0.77, :mod:`app.ocr_find_replace`),
-- vision-text promotion (v0.75, :mod:`app.web.routes.ocr_vision_replace`)
-- and any future per-shot manual edit. Until v0.92 the previous value
-- was lost the moment the ``UPDATE`` fired — the operator could see a
-- regex had eaten too much only by ``git blame``-style detective work
-- in the FTS index, with no way to revert.
--
-- This migration introduces a tiny append-only snapshot table that the
-- three write paths populate *before* each overwrite. A revert is then
-- a single ``UPDATE screenshots SET ocr_text = prev_text`` keyed by the
-- snapshot id — no special re-encoding, no FTS replay logic, because
-- the existing ``screenshots_au`` trigger already keeps FTS consistent
-- on every UPDATE.
--
-- Columns
-- -------
--   * ``shot_id``     — the ``screenshots.id`` whose text was replaced.
--                       No FK is declared (the codebase consistently
--                       keeps audit/history tables FK-free so that a
--                       future bulk-delete of orphaned screenshots
--                       doesn't cascade through history) but the column
--                       is indexed because :func:`list_for_shot` is the
--                       only common access pattern.
--   * ``prev_text``   — the ``ocr_text`` value about to be overwritten.
--                       NOT NULL because callers are required to skip
--                       the snapshot when the prior text is NULL/empty
--                       (a revert would be a no-op anyway).
--   * ``replaced_at`` — ISO-8601 wall-clock from ``datetime('now')``,
--                       matching the format used by :file:`037_audit_log.sql`
--                       and :file:`075_dashboard_widgets.sql`.
--   * ``reason``      — free-form slug identifying the write path:
--                       ``"find_replace"``, ``"vision_replace"``,
--                       ``"manual"``. Stored for the revert UI's
--                       human-readable column and for ``/audit``
--                       cross-referencing; never parsed.
--
-- ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` keep
-- the migration idempotent across re-runs of ``init_database``.

CREATE TABLE IF NOT EXISTS ocr_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shot_id INTEGER NOT NULL,
    prev_text TEXT NOT NULL,
    replaced_at TEXT NOT NULL DEFAULT (datetime('now')),
    reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_ocr_history_shot_id
    ON ocr_history(shot_id);
