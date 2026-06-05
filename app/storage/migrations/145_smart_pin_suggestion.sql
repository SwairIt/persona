-- 145 — smart-pin LLM auto-flagger suggestions table.
--
-- The smart-pin worker (``app.workers.smart_pin_worker`` + LLM stage
-- ``app.llm.smart_pin``) scans yesterday's un-pinned screenshots once a
-- day and asks the configured LLM "which of these look important?".
-- The model returns 1–3 candidates with a short reason and a 0..1
-- confidence score; we persist them as *suggestions* rather than
-- flipping the real ``screenshots.tier`` to ``pinned`` straight away.
--
-- This separates two concerns:
--
--   1. LLM-driven heuristics — fast to iterate, may be wrong, must
--      remain user-reviewable. They live in this table.
--   2. The actual "pinned" tier — a hard guarantee that the row will
--      survive tier-sweep retention. Only user action (accepting a
--      suggestion from ``/memory/smart-pins`` or using the normal pin
--      button) flips that bit. The accept route writes ``accepted_at``
--      here and calls the existing ``pin_screenshot`` helper.
--
-- Same pattern as the dup-finder suggestions surface — a soft layer in
-- a sibling table, no destructive change to the screenshots row until
-- the user confirms.
--
-- ``score`` is the model's self-reported confidence; we keep it for UX
-- (sort by descending importance) and post-hoc calibration. ``reason``
-- is the one-sentence justification displayed in the review UI.
--
-- ``created_at`` defaults to ``datetime('now')`` so an INSERT only
-- needs to provide the foreign key, reason and score. The
-- ``UNIQUE(screenshot_id, created_at)`` constraint guards against the
-- worker firing twice in the same second and double-inserting; in
-- practice the worker fires once per day, so this is belt-and-braces.
--
-- ``accepted_at`` and ``dismissed_at`` are mutually exclusive nullable
-- columns: a pending row has both NULL, an accepted row has the former
-- set, a dismissed row has the latter. The partial index
-- ``idx_smart_pin_pending`` lets the dashboard list "still pending"
-- suggestions cheaply without scanning the full history of past
-- decisions.

CREATE TABLE IF NOT EXISTS smart_pin_suggestion (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    screenshot_id INTEGER NOT NULL REFERENCES screenshots(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    accepted_at TEXT,
    dismissed_at TEXT,
    UNIQUE(screenshot_id, created_at)
);

CREATE INDEX IF NOT EXISTS idx_smart_pin_pending
    ON smart_pin_suggestion(accepted_at, dismissed_at)
    WHERE accepted_at IS NULL AND dismissed_at IS NULL;
