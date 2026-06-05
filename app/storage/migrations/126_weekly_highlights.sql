-- v1.46 — Weekly LLM-curated highlights.
--
-- Complements ``weekly_card.llm_summary`` (one narrative paragraph, see
-- 119_weekly_card_llm.sql) with a curated *list* of 5-7 standout
-- moments per week. Each row references one specific source artefact —
-- a screenshot (``shot``), a free-text ``screenshot_note`` (``note``),
-- or a ``capture_session`` (``session``) — and carries a one-sentence
-- reason explaining why the LLM picked it.
--
-- Column contract
-- ---------------
--   * ``week_start`` — ISO ``YYYY-MM-DD`` of the Monday of the target
--     week. Matches the convention used by ``weekly_card.week_start``
--     so the highlight rows can be JOINed back to their tier-2 card
--     without a date-coercion dance.
--   * ``rank`` — 1..7 ordinal position within the week. The LLM is
--     prompted to emit picks in importance order, so ``rank=1`` is the
--     most interesting pick. ``UNIQUE(week_start, rank)`` is the
--     idempotency key — a re-run for the same week will collide on
--     this constraint and fail loudly rather than silently
--     duplicating picks.
--   * ``source_kind`` — discriminator for ``source_id``. Constrained
--     to one of ``shot``/``note``/``session`` via a CHECK so the
--     renderer can route to the right detail page without a magic
--     string elsewhere in the codebase.
--   * ``source_id`` — primary key of the referenced row in the table
--     implied by ``source_kind``. Deliberately NOT a foreign key:
--     screenshots may be tier-decayed away, notes deleted, and we
--     still want to keep the highlight + ``reason`` text as a
--     historical record. Renderers must defensively handle missing
--     source rows.
--   * ``title`` — short label (typically window title, note excerpt
--     or session app name) the LLM picked, surfaced in the
--     ``/memory/highlights`` list as the headline.
--   * ``reason`` — one-sentence justification, the qualitative "why
--     this matters" the LLM is asked to produce alongside each pick.
--   * ``created_at`` — generation timestamp; useful when the operator
--     wants to know which LLM run produced a given pick.
--
-- The single index on ``week_start`` covers the only read pattern the
-- UI has — "list this week's picks ordered by rank" — without paying
-- for a wider covering index. ``rank`` is part of the UNIQUE
-- constraint so it is already indexed implicitly for the per-week
-- ``ORDER BY rank ASC`` query plan.

CREATE TABLE IF NOT EXISTS weekly_highlight (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL,
    rank INTEGER NOT NULL,
    source_kind TEXT NOT NULL
        CHECK (source_kind IN ('shot', 'note', 'session')),
    source_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(week_start, rank)
);

CREATE INDEX IF NOT EXISTS idx_weekly_highlight_week
    ON weekly_highlight(week_start);
