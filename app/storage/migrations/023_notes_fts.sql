-- FTS5 full-text index over screenshot_notes.body.
--
-- Stores one FTS row per note: body is indexed, note_id is carried alongside
-- as an UNINDEXED column so result rows can be linked back to the underlying
-- screenshot/note. We deliberately do NOT use `content=` / `content_rowid=`
-- (external-content mode) because note_id has no matching column on the
-- screenshot_notes table — it is a logical alias for screenshot_id, and
-- external-content mode would try to pull it from the parent table.
--
-- The underlying table is screenshot_notes(screenshot_id PK, body, ...);
-- "id" in the v0.26 spec maps to screenshot_id here. Triggers below keep the
-- index in sync on insert/delete/update of screenshot_notes — we use the
-- rowid (== screenshot_id) as the FTS rowid, so 'INSERT OR REPLACE' (and
-- the rowid-targeted delete) cleanly upserts every change.

CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
    body,
    note_id UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);

-- After INSERT on screenshot_notes, mirror the new row into notes_fts.
-- INSERT OR REPLACE on rowid so a redundant backfill / replayed trigger
-- does not double the row.
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON screenshot_notes
BEGIN
    INSERT OR REPLACE INTO notes_fts(rowid, body, note_id)
    VALUES (new.screenshot_id, COALESCE(new.body, ''), new.screenshot_id);
END;

-- After DELETE on screenshot_notes, drop the matching FTS row by rowid.
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON screenshot_notes
BEGIN
    DELETE FROM notes_fts WHERE rowid = old.screenshot_id;
END;

-- After UPDATE, replace the FTS row in-place via rowid-keyed upsert. If the
-- screenshot_id itself changed (extremely unlikely — it's a PK), the old
-- entry is also removed.
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON screenshot_notes
BEGIN
    DELETE FROM notes_fts WHERE rowid = old.screenshot_id AND old.screenshot_id != new.screenshot_id;
    INSERT OR REPLACE INTO notes_fts(rowid, body, note_id)
    VALUES (new.screenshot_id, COALESCE(new.body, ''), new.screenshot_id);
END;

-- Initial backfill: copy every existing note into the FTS index. INSERT OR
-- REPLACE on rowid keeps this idempotent — re-applying the migration just
-- refreshes each row in place rather than duplicating.
INSERT OR REPLACE INTO notes_fts(rowid, body, note_id)
SELECT screenshot_id, COALESCE(body, ''), screenshot_id
FROM screenshot_notes;
