-- v1.54 — Tag canonicaliser: persisted alias trail for case/whitespace/unicode merges.
--
-- Background
-- ----------
-- The pre-store overlay in :mod:`app.tag_aliases` is a *forward-looking*
-- fix: every fresh write of a tag funnels through ``resolve`` so a known
-- alias is rewritten to its canonical name before it ever hits the
-- ``tags`` row. That works for new writes but does nothing about the
-- mountain of *legacy* rows already split across case-different,
-- whitespace-different and unicode-different variants of the same
-- concept (``StandUp`` / ``stand up`` / ``stand_up`` / ``STAND‑UP``):
-- every variant survives as a separate ``tags`` row with its own
-- screenshot set, and search-by-name surfaces only one of them at a
-- time.
--
-- :mod:`app.tag_canonicaliser` is the *retroactive* sweep that fixes
-- that. It scans every distinct ``tags.name`` currently in use, groups
-- variants by their normalised form (strip + lowercase + NFKC + collapse
-- whitespace to underscores), picks the most-used raw spelling in each
-- cluster as the canonical, and rewrites the legacy rows so the
-- canonical owns every screenshot the cluster ever touched. This table
-- is the audit trail of every such rewrite.
--
-- Schema
-- ------
--   * ``id``         — surrogate key. The same alias can legitimately
--                      appear more than once across separate canonicaliser
--                      runs (an operator manually re-introduces the
--                      legacy spelling and another sweep folds it again),
--                      so we want an autoincrement id rather than the
--                      pre-store overlay's ``alias`` PK that
--                      :file:`085_tag_aliases.sql` uses.
--   * ``alias``      — the raw spelling that was rewritten. ``UNIQUE``
--                      because once a sweep has consumed an alias the
--                      rows it points at are gone, so any future sweep
--                      that sees the *same* raw spelling means an
--                      operator re-typed it — surface that as a unique-
--                      constraint conflict the application layer can
--                      log and skip rather than silently double-applying.
--   * ``canonical``  — the surviving spelling for the cluster. Indexed
--                      separately so the admin UI's "show every alias
--                      that mapped to this canonical" reverse lookup is
--                      a bounded scan rather than a full table sweep.
--   * ``applied_at`` — ISO-8601 UTC wall-clock of the moment the rewrite
--                      committed; matches the convention used by 074 /
--                      076 / 078 / 085 for audit trails.
--
-- ``CREATE TABLE IF NOT EXISTS`` keeps the migration idempotent across
-- re-runs of :func:`app.storage.db.init_database` (the runner replays
-- every ``*.sql`` in lexicographic order on every boot). The supporting
-- index on ``canonical`` matches the access pattern of the admin UI.

CREATE TABLE IF NOT EXISTS tag_alias (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias TEXT NOT NULL UNIQUE,
    canonical TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tag_alias_canonical ON tag_alias(canonical);
