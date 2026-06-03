-- v0.55 — per-shot share link read receipts.
--
-- Every successful GET to ``/shot/share/{shot_id}/{token}`` records a row
-- here so the owner can see, on the admin share page, when a recipient
-- actually opened the link. We deliberately keep only a coarse signal:
--   * ``visited_at`` is the moment the public viewer rendered (UTC),
--   * ``ua`` is the User-Agent truncated to 200 chars (header is unbounded
--     and some bots spam multi-kB strings),
--   * ``ip_prefix`` is the first two octets of the client IP (e.g. ``192.168``)
--     — enough to spot "same network" patterns without ever persisting an
--     identifying address. IPv6 stores the first two ``:``-separated groups.
--
-- We do NOT add a foreign key onto ``screenshots(id)``: the share viewer
-- already guards on the screenshot still existing before recording, and
-- keeping the table FK-free lets a future ``recycle_bin`` purge of the
-- screenshot leave historical visit rows intact for auditing.
--
-- The ``idx_share_visit_shot_id`` index supports the admin page's
-- ``ORDER BY visited_at DESC LIMIT 50`` lookup by ``shot_id``.
--
-- ``IF NOT EXISTS`` clauses keep the migration idempotent across re-runs.

CREATE TABLE IF NOT EXISTS share_visit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shot_id INTEGER NOT NULL,
    visited_at TEXT NOT NULL DEFAULT (datetime('now')),
    ua TEXT,
    ip_prefix TEXT
);

CREATE INDEX IF NOT EXISTS idx_share_visit_shot_id
    ON share_visit(shot_id);
