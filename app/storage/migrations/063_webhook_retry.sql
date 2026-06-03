-- v0.64 — retry queue for failed outbound webhook deliveries.
--
-- The dispatcher (``app.webhooks.dispatcher``) historically logged a
-- failure row into ``webhooks.last_error`` and moved on; a flaky
-- receiver, a transient TCP reset or a 5xx from the upstream meant the
-- caller permanently lost the event. This table stores the raw envelope
-- bytes so a background worker can re-POST them later with exponential
-- backoff.
--
-- One row per (webhook, undelivered event). The same webhook can have
-- many queued events in-flight simultaneously — we don't dedupe; if the
-- dispatcher chose to fire it twice we replay it twice.
--
-- ``body`` is the *exact* HTTP request body — including the outer
-- ``{"event", "ts", "payload"}`` envelope and the original ISO timestamp.
-- This matters for the signature: the worker re-signs with the per-row
-- webhook secret on each replay, but it does NOT re-stamp ``ts``, so a
-- receiver that has clock-skew rejection on ``X-Persona-Timestamp`` may
-- drop very old replays. That's the intended trade-off: the dispatcher's
-- happens-at-emission timestamp is the authoritative one.
--
-- ``attempts`` starts at 0 *before* the first retry runs — i.e. the row
-- represents work the dispatcher already tried once and failed. The
-- retry worker increments on each pass; ``app.webhook_retry`` caps it at
-- ``MAX_ATTEMPTS`` (8) and gives up.
--
-- ``next_attempt_at`` is in UTC, ISO-formatted via SQLite's
-- ``datetime('now')`` — the same convention every other table in this
-- migration set uses. The worker's WHERE clause compares against
-- ``datetime('now')`` directly so the column never needs to be parsed
-- in Python on the hot path.

CREATE TABLE IF NOT EXISTS webhook_retry_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    webhook_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    body BLOB NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Worker picks the earliest-due, not-yet-capped rows; this composite
-- index turns the polling query into an index range scan instead of a
-- full table scan once the queue starts carrying any backlog.
CREATE INDEX IF NOT EXISTS idx_webhook_retry_due
    ON webhook_retry_queue (next_attempt_at, attempts);
