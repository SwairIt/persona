-- Durable Telegram update inbox and precise autowake attempt accounting.
--
-- Telegram payloads are intentionally not stored.  update_id is enough to
-- suppress a replay after a crash without retaining private message content.
CREATE TABLE IF NOT EXISTS telegram_update_inbox (
    update_id           INTEGER PRIMARY KEY CHECK (update_id >= 0),
    status              TEXT NOT NULL DEFAULT 'processing'
                        CHECK (status IN ('processing', 'processed', 'failed')),
    holder_id           TEXT,
    lease_until         TEXT,
    outcome             TEXT,
    first_seen_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at        TEXT,
    CHECK (
        (status = 'processing' AND holder_id IS NOT NULL AND lease_until IS NOT NULL)
        OR
        (status != 'processing' AND holder_id IS NULL AND lease_until IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_telegram_update_inbox_status
    ON telegram_update_inbox(status, lease_until, update_id);

-- ``attempts`` is incremented when delivery begins.  An expired lease must
-- increment only when it died before start_attempt; otherwise one physical
-- attempt would be counted twice.
ALTER TABLE autowake_outbox ADD COLUMN attempt_started_at TEXT;
