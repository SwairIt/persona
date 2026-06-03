-- v1.1 feature 2/3 — audio note attachments.
--
-- Background
-- ----------
-- The standalone ``notes`` table (originally created in
-- ``039_inbox_notes.sql``) only stored the textual body of a note. The
-- v1.1 polish pass needs to let the operator pin one or more *audio*
-- snippets onto a note — a voice memo dropped from a phone, a quick
-- dictation recorded in the browser, or an OGG file dragged out of
-- another app.
--
-- The audio file bytes live on disk under the per-user data directory
-- (``<data_dir>/note_attachments/``) — same pattern as
-- ``import_screenshot.py`` uses for manual screenshots. Only the
-- metadata + the relative ``path`` lives in SQLite, so a database dump
-- stays small and the heavy binary blobs sit on the filesystem where
-- they belong.
--
-- Schema
-- ------
-- One table, ``note_attachment``, that fully describes a single audio
-- file attached to exactly one note. The columns are deliberately
-- denormalised: storing both ``filename`` (operator-friendly, surfaced
-- in HTML / JSON) and ``path`` (relative on-disk location used by the
-- streaming endpoint) avoids a join against any "blob store" table.
--
--   * ``id``           — AUTOINCREMENT integer PK. Used as the URL path
--                        segment in ``/api/note-attachment/{att_id}/…``.
--                        AUTOINCREMENT (not just INTEGER PRIMARY KEY) so
--                        ids are *never* reused after a DELETE, which
--                        would otherwise make audit-log entries
--                        ambiguous across CASCADE.
--   * ``note_id``      — FK back to ``notes(id)``. ``ON DELETE CASCADE``
--                        so a removed note also drops every audio
--                        attachment row — the route is still
--                        responsible for unlinking the matching file on
--                        disk before deleting the note, but this guards
--                        against orphan rows when a stray DELETE slips
--                        through other paths.
--   * ``filename``     — operator-facing name. Sanitised by the route
--                        (same scrub as ``import_screenshot.py``) before
--                        landing here so this column is always safe to
--                        surface in HTML.
--   * ``mime``         — MIME type declared by the upload **and**
--                        validated by the route (must start with
--                        ``audio/``). Stored so the streaming endpoint
--                        can echo it back as ``Content-Type`` without
--                        re-sniffing the file.
--   * ``size_bytes``   — exact byte count written to disk. Surfaced in
--                        the JSON listing so the UI can render a
--                        "1.4 MB" hint next to each attachment.
--   * ``path``         — relative on-disk path under ``<data_dir>``.
--                        Stored relative (not absolute) so the database
--                        is portable across machines / backups — the
--                        route prepends ``settings.data_dir`` at read
--                        time.
--   * ``created_at``   — ISO timestamp (``datetime('now')`` matches the
--                        shape used by every adjacent table).
--
-- Indexes
-- -------
--   * ``idx_note_attachment_note`` — the listing endpoint always filters
--     by ``note_id`` and we expect a single note to have a small handful
--     of attachments, so a covering index on the FK column is the right
--     trade-off (cheap to maintain, eliminates a scan on the GET).
--   * ``idx_note_attachment_created_at`` — chronological ordering of all
--     attachments without forcing a sort over the heap when an admin
--     view eventually wants "most-recently-attached" globally.
--
-- ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` keep
-- the migration idempotent across re-runs of ``init_database``.

CREATE TABLE IF NOT EXISTS note_attachment (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    note_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    mime TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(note_id) REFERENCES notes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_note_attachment_note
    ON note_attachment(note_id);

CREATE INDEX IF NOT EXISTS idx_note_attachment_created_at
    ON note_attachment(created_at DESC);
