-- v1.44 — focus-aware in-browser notification queue.
--
-- The previous push-notif loop (v0.66) replayed reminder rows. v1.44
-- introduces a *dedicated* notification table the rest of the codebase
-- can push structured events into (capture stopped, OCR backlog spike,
-- a long-read clipped, etc.) and a route-side widget surfaces them.
--
-- Schema notes:
--   notification.severity   — three-valued enum (info/warn/error)
--                             enforced by CHECK so a typo at the
--                             producer call site fails loudly instead
--                             of silently rendering as the default tone.
--   notification.seen_at    — NULL while unread; stamped on the first
--                             POST /seen call. We never delete rows —
--                             the badge clears by transitioning to a
--                             timestamp, which keeps the audit trail
--                             intact for ``mark_all_seen`` regression
--                             investigations.
--   idx_notification_created — ``ORDER BY created_at DESC`` is the hot
--                              path for the bell-list query.
--   idx_notification_unseen  — partial index on the badge filter
--                              (``WHERE seen_at IS NULL``) — keeps the
--                              count query cheap as the table grows.

CREATE TABLE IF NOT EXISTS notification (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    link TEXT,
    severity TEXT NOT NULL DEFAULT 'info'
        CHECK (severity IN ('info', 'warn', 'error')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    seen_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_notification_created
    ON notification(created_at);

CREATE INDEX IF NOT EXISTS idx_notification_unseen
    ON notification(seen_at) WHERE seen_at IS NULL;
