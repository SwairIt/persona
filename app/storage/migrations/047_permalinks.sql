-- v0.48 — shareable permalinks for any in-app URL state.
--
-- A "permalink" is a short, opaque slug (8-char base36) that maps to a
-- relative ``target_url`` already inside Persona. Operators paste the
-- current ``location.href`` (path + query string + hash) into the admin
-- form and get back ``/go/{slug}`` — a stable, easy-to-share URL that
-- 302-redirects to the original page state. ``label`` is an optional
-- human-readable note ("launch deck Q3 search", "april focus filter")
-- so the admin table reads like a directory of saved page states
-- instead of a sea of opaque tokens.
--
-- ``hits`` counts redirects so we can spot dead links and popular ones
-- at a glance from the admin table. It is bumped atomically by the
-- redirect route — see :func:`app.permalinks.bump_hits`.
--
-- Validation rule (enforced in Python, not by the schema): the helper
-- module rejects any ``target_url`` that does not start with ``/`` so
-- this table cannot be turned into an open-redirect — a permalink can
-- only point at another Persona page. SQLite has no CHECK that would
-- usefully cover the same ground without false positives, so the
-- guard lives next to the only INSERT site.

CREATE TABLE IF NOT EXISTS permalink (
    slug TEXT PRIMARY KEY,
    target_url TEXT NOT NULL,
    label TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    hits INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_permalink_created_at ON permalink(created_at);
