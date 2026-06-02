-- v0.35 — opt-in clipboard history capture.
--
-- The clipboard worker (``app.workers.clipboard_worker``) polls the OS
-- clipboard while the ``clipboard_history_enabled`` setting is True and
-- stores each *new* text snippet here so the user can search it later.
-- Capture is opt-in for privacy reasons — the default is OFF.
--
-- ``hash`` is the SHA-256 hex digest of the raw text, used to dedupe
-- runs of identical clipboard reads (the OS will happily report the
-- same buffer hundreds of times in a row otherwise). We index it so the
-- worker can answer "have I seen this snippet already?" in O(log n).
--
-- ``length`` is the character count of the *original* (pre-redaction)
-- text. The stored ``text`` column has the redaction rules from
-- ``app.redaction`` applied, so length lets the UI show how much was
-- actually copied even when most of it is masked as ``***``.
--
-- ``app_name`` is the foreground app at the moment of capture (best
-- effort; may be NULL if the active window probe failed). Useful for
-- filtering / grouping in the UI.
--
-- ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` keep
-- the migration idempotent across re-runs of ``init_database``.

CREATE TABLE IF NOT EXISTS clipboard_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    captured_at TEXT NOT NULL DEFAULT (datetime('now')),
    text TEXT NOT NULL,
    length INTEGER NOT NULL,
    app_name TEXT,
    hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_clipboard_event_captured_at
    ON clipboard_event (captured_at DESC);

CREATE INDEX IF NOT EXISTS idx_clipboard_event_hash
    ON clipboard_event (hash);
