-- v1.3 feature 1/3 — tag aliases (many human-friendly names → one canonical tag).
--
-- Background
-- ----------
-- The tag store (``tags``) has always treated names as the public
-- identity: the LLM auto-tagger (:func:`app.llm.suggest_tags`), the
-- phrase-rule worker (:mod:`app.ocr_phrase_tags`) and the manual
-- tagger all funnel through :func:`app.storage.tags.create_tag`, which
-- is idempotent on ``name``. That means two semantically-identical
-- labels — say ``standup`` and ``daily-standup`` — accrete as two
-- separate rows with disjoint screenshot sets, splitting what the
-- operator thinks of as a single concept across two facets in the
-- search UI.
--
-- ``tag_alias`` is a *pre-store* overlay: before the tagger writes
-- anything, it routes the candidate name through
-- :func:`app.tag_aliases.resolve`, which returns the canonical form if
-- an alias row exists and the input unchanged otherwise. The
-- ``screenshot_tags`` rows therefore never see the alias spelling at
-- all — only the canonical tag id is ever materialised — so a search
-- by the canonical name surfaces everything the user expects in one
-- bucket.
--
-- Schema
-- ------
--   * ``alias``      — PRIMARY KEY, the human-friendly variant
--                      (``daily-standup``, ``standups``, ``stand-up``).
--                      Trimmed + lowercased at the application layer
--                      before INSERT so the comparison in
--                      :func:`resolve` is a pure equality lookup with
--                      no per-row ``LOWER()`` cost.
--   * ``canonical``  — NOT NULL, the tag name that downstream code
--                      should actually use. Free-form text; the alias
--                      table is decoupled from ``tags`` deliberately so
--                      an operator can pre-declare a canonical name
--                      before any screenshot has been tagged with it
--                      (the row in ``tags`` is created lazily by
--                      :func:`app.storage.tags.create_tag` on first use).
--   * ``created_at`` — ISO-8601 wall-clock, matches the convention used
--                      by 074/076/078 for audit trails.
--
-- ``CREATE TABLE IF NOT EXISTS`` keeps the migration idempotent across
-- re-runs of :func:`app.storage.db.init_database`. The supporting index
-- on ``canonical`` makes the admin UI's "show every alias that maps to
-- this tag" reverse-lookup an indexed scan rather than a full sweep.

CREATE TABLE IF NOT EXISTS tag_alias (
    alias TEXT PRIMARY KEY,
    canonical TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tag_alias_canonical
    ON tag_alias (canonical);
