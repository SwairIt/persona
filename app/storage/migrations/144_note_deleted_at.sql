-- v1.49 — Soft-delete column on ``notes`` for the stale-note pruner tool.
--
-- The stale-note pruner (``app/stale_note_pruner.py`` + the
-- ``/admin/stale-notes`` admin page) flags inbox notes whose body is
-- empty or whitespace-only and older than a configurable age cutoff
-- (default 90 days). Such rows are leftovers from aborted note
-- creations or cleared bodies that were never deleted — they clutter
-- search/listing without carrying any user content.
--
-- The pruner stamps ``deleted_at`` instead of physically removing the
-- row. Same pattern the recycle bin (migration 041) and the dup-finder
-- (migration 128) already use elsewhere, and the operator can recover
-- a falsely-flagged row by clearing the column manually. The audit
-- trail of "what got pruned when" lives in ``audit_log`` via
-- :func:`app.audit.log_action`; this column just gives the listing
-- queries a fast filter.
--
-- Nullable, no default: existing rows keep ``deleted_at = NULL``
-- (active). Indexed because every notes-listing query will want to
-- skip soft-deleted rows quickly, and the pruner itself filters on
-- ``deleted_at IS NULL`` when scanning for candidates.

ALTER TABLE notes ADD COLUMN deleted_at TEXT;

-- v1.66 — IF NOT EXISTS чтобы повторный прогон миграции (например
-- после рестарта uvicorn на DB с уже применённой схемой) не падал.
-- Без этого migration-runner ловит только 'duplicate column name'.
CREATE INDEX IF NOT EXISTS idx_notes_deleted ON notes(deleted_at);
