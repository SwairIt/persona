-- v0.37 — standalone markdown notes inbox.
--
-- Notes that arrive via the ``./data/inbox/`` watch-folder live in their
-- own ``notes`` table, separate from ``screenshot_notes`` (those are
-- pinned to a specific screenshot row). Tags are reused from the
-- existing ``tags`` table via a dedicated ``note_tags`` join — we
-- deliberately keep the tag vocabulary shared with screenshots so a
-- search for ``#meeting`` returns both notes and tagged screenshots.
--
-- ``IF NOT EXISTS`` keeps the migration idempotent across re-runs.

CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    body TEXT NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_notes_created_at ON notes(created_at);

CREATE TABLE IF NOT EXISTS note_tags (
    note_id INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (note_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_note_tags_tag ON note_tags(tag_id);
