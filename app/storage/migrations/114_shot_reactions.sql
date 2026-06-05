-- v1.34 — Emoji reactions on screenshots (love / important / funny / wtf / idea).
--
-- Each screenshot can accumulate a small fixed-vocabulary set of emoji
-- reactions used as a lightweight "what is this shot to me?" signal:
-- heart for love, star for important, laugh for funny, wtf for the
-- surprise pile, lightbulb for "this sparked an idea". The five-emoji
-- vocabulary is enforced in :mod:`app.shot_reactions` (Python side) — at
-- the schema level we only require that the (screenshot_id, emoji) pair
-- is unique, so a user can either *have* a given reaction on a shot or
-- not. There's no count column — the reactions are toggles, not
-- thumbs-up tallies — and exactly one row per (shot, emoji) means
-- toggle_reaction() can use ``INSERT OR IGNORE`` followed by ``DELETE``
-- without ever needing an UPSERT.
--
-- ``ON DELETE CASCADE`` cleans these rows when the parent screenshot is
-- pruned by retention sweeps; foreign-key enforcement is enabled by the
-- runtime PRAGMA in ``app/storage/db.py``.
--
-- The two single-column indexes are sized for the two reads we do:
-- :func:`list_reactions_for_shot` filters by ``screenshot_id`` (covered
-- by the implicit ``UNIQUE`` index too, but a dedicated index keeps
-- query plans stable across SQLite versions), while
-- :func:`top_reacted_shots` aggregates ``COUNT(*)`` per
-- ``screenshot_id`` optionally filtered by ``emoji`` — both columns
-- benefit from their own index.

CREATE TABLE IF NOT EXISTS shot_reaction (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    screenshot_id INTEGER NOT NULL
        REFERENCES screenshots(id) ON DELETE CASCADE,
    emoji TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(screenshot_id, emoji)
);

CREATE INDEX IF NOT EXISTS idx_shot_reaction_shot
    ON shot_reaction(screenshot_id);

CREATE INDEX IF NOT EXISTS idx_shot_reaction_emoji
    ON shot_reaction(emoji);
