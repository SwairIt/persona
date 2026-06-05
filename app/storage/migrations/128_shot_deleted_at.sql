-- v1.27 — Soft-delete column on ``screenshots`` for the dup-finder admin tool.
--
-- The image-similarity duplicate finder
-- (``app/dup_finder.py`` + ``GET /admin/dup-finder``) lets the operator
-- bulk-flag near-identical shots that the capture-time pHash deduper
-- missed (e.g. minor pixel diff that exceeded the runtime hamming
-- threshold). The "Delete others, keep K" action soft-deletes the
-- rejected rows by stamping ``deleted_at`` instead of physically
-- removing them — same pattern the recycle bin uses (migration 041),
-- but inline on the row so it's queryable alongside ``captured_at``
-- without a join.
--
-- Nullable, no default: existing rows keep ``deleted_at = NULL``
-- (active). Indexed because the dup-finder query and the
-- about-to-follow visibility filters on the timeline both want to
-- skip soft-deleted rows quickly.

ALTER TABLE screenshots ADD COLUMN deleted_at TEXT;

CREATE INDEX IF NOT EXISTS idx_screenshots_deleted_at
    ON screenshots(deleted_at);
