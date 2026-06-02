-- v0.40 — soft-delete recycle bin.
--
-- Bulk-delete and the screenshot detail "delete" button no longer wipe
-- rows straight out of ``screenshots`` / ``notes``; they instead route
-- through :func:`app.recycle.soft_delete_screenshot` /
-- :func:`app.recycle.soft_delete_note` which serialise the row to JSON
-- and park it here. The retention worker calls
-- :func:`app.recycle.purge_expired` once per loop iteration to hard-delete
-- (and unlink any thumbnail) anything older than
-- ``settings.recycle_retention_days``.
--
-- ``payload`` is the row JSON so :func:`restore` can re-insert verbatim
-- without joining other tables. ``thumbnail_path`` is duplicated outside
-- the JSON so :func:`purge_expired` can unlink the file without parsing.
--
-- ``IF NOT EXISTS`` keeps the migration idempotent across re-runs.

CREATE TABLE IF NOT EXISTS recycle_bin (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('screenshot', 'note')),
    original_id INTEGER NOT NULL,
    payload TEXT NOT NULL,
    deleted_at TEXT NOT NULL DEFAULT (datetime('now')),
    thumbnail_path TEXT
);

CREATE INDEX IF NOT EXISTS idx_recycle_bin_kind_deleted_at
    ON recycle_bin(kind, deleted_at);
