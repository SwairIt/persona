-- v0.45 — opt-in per-note body encryption.
--
-- Each row in the standalone ``notes`` table (created in
-- ``039_inbox_notes.sql``) gets two new fields:
--
--   * ``encrypted``  — boolean (0/1) flag. When ``1`` the plaintext has
--     been moved out of ``body`` and into ``ciphertext``; readers must
--     refuse to display ``body`` and instead prompt for the master
--     password before calling :func:`app.encrypted_notes.decrypt_note`.
--   * ``ciphertext`` — Fernet envelope produced by
--     :mod:`app.encrypted_notes`. The first 16 bytes are the per-note
--     PBKDF2 salt; the remainder is the Fernet token. ``NULL`` for
--     ordinary plaintext rows.
--
-- ``body`` keeps its ``NOT NULL`` constraint (SQLite's ``ALTER TABLE``
-- cannot drop it without a table rebuild, and the task spec restricts
-- this migration to ``ADD COLUMN`` statements). When a note is
-- encrypted :func:`encrypt_note` writes an empty string into ``body``
-- — every reader gates on the ``encrypted`` flag first, so the empty
-- string is never user-visible.
--
-- Adding ``encrypted`` with ``DEFAULT 0`` means every pre-existing row
-- comes out of the migration as a plaintext note — no backfill needed.
--
-- SQLite has no ``ALTER TABLE … ADD COLUMN IF NOT EXISTS`` so the
-- migration relies on the runner only executing each file once per DB
-- (same contract as ``042_webhook_event_filter.sql``).

ALTER TABLE notes ADD COLUMN encrypted INTEGER NOT NULL DEFAULT 0;

ALTER TABLE notes ADD COLUMN ciphertext BLOB;

-- Helps the listing route filter / count encrypted rows without a full
-- table scan. Partial index keeps it tiny — typically a handful of rows.
CREATE INDEX IF NOT EXISTS idx_notes_encrypted ON notes(encrypted) WHERE encrypted = 1;
