-- v0.70 — per-shot lock guard against accidental deletion.
--
-- The recycle bin (v0.40) already gives the user a grace window to undo
-- a bulk-delete, but a lock is a stronger statement: "this shot is too
-- important to even *risk* losing to a typo'd FTS query or a stray
-- click on the context-menu Delete entry." A locked row is excluded
-- from :mod:`app.bulk_delete` and rejected outright by
-- :func:`app.recycle.soft_delete_screenshot` (raises ``ShotLocked``).
-- The detail page exposes a Lock/Unlock toggle, and the right-click
-- menu disables its Delete action when the thumbnail wrapper carries
-- ``data-locked="1"``.
--
-- ``locked`` is INTEGER NOT NULL DEFAULT 0 — SQLite has no boolean
-- type, so we stick with the same 0/1 convention used by
-- ``screenshots.is_private``. The default of 0 keeps every pre-existing
-- row unlocked: locking is strictly opt-in, never retroactive.
--
-- SQLite's ``ALTER TABLE ... ADD COLUMN`` has no ``IF NOT EXISTS``
-- form. :func:`app.storage.db.init_database` catches the
-- ``duplicate column name`` error per-statement (the v0.51 split path),
-- so re-running this migration on an already-upgraded DB silently
-- no-ops while a genuinely fresh install gets the column.

ALTER TABLE screenshots ADD COLUMN locked INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_screenshots_locked
    ON screenshots(locked) WHERE locked = 1;
