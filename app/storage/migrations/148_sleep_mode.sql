-- v1.62 — Sleep-mode auto-detector event log.
--
-- Sister table to ``meeting_event`` (v1.19) and ``privacy_skip_event``
-- (v1.40): an append-only audit trail of capture-loop short-circuits,
-- this time for "user walked away from the keyboard for long enough
-- that we paused screenshots entirely". Lives next to those siblings
-- in the migrations directory so an operator inspecting the schema
-- finds them grouped.
--
-- Schema
-- ------
--   * ``id``           — surrogate key, INTEGER PRIMARY KEY AUTOINCREMENT
--                        so SQLite never reuses ids even after rows are
--                        purged by a future retention sweep. The
--                        settings page renders the most recent N rows
--                        sorted by id DESC (cheaper than occurred_at
--                        DESC because the primary index covers it).
--   * ``occurred_at``  — wall-clock of the transition, ISO-8601 UTC
--                        via ``datetime('now')``. SQLite stores it as
--                        TEXT so jq / sqlite3 CLI render it human-
--                        readably without a timezone dance. Indexed
--                        (see below) for the future "events in last
--                        24h" widget.
--   * ``state``        — ``'sleep'`` when the loop entered sleep mode,
--                        ``'wake'`` when it left. Enforced by CHECK so
--                        a bad caller (or a typo in a future migration
--                        backfill) fails at write time instead of
--                        poisoning the audit log. Two-valued by design
--                        — we deliberately do not log "still sleeping"
--                        ticks because the capture-loop's
--                        ``_last_sleep_state`` cache guarantees the
--                        steady state never reaches this INSERT.
--   * ``idle_seconds`` — observed ``seconds_since_last_input`` at the
--                        moment of the transition, rounded to integer
--                        seconds. Useful for "user came back after 23
--                        minutes" UX without re-deriving from the
--                        threshold setting.
--
-- Indexes
-- -------
-- A single index on ``occurred_at`` covers the "events in the last
-- N hours" query the JSON endpoint may grow into. The primary key
-- already serves the "newest N" path used by the settings page today.
--
-- ``CREATE TABLE IF NOT EXISTS`` keeps this idempotent on re-runs
-- (:func:`app.storage.db.init_database` replays every ``*.sql`` in
-- lexicographic order on every boot).

CREATE TABLE IF NOT EXISTS sleep_mode_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL DEFAULT (datetime('now')),
    state TEXT NOT NULL CHECK (state IN ('sleep', 'wake')),
    idle_seconds INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sleep_mode_event_at
    ON sleep_mode_event(occurred_at);
