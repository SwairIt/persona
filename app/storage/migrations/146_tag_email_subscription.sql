-- v1.61 — Per-tag weekly email digest subscriptions.
--
-- The v1.59 weekly-digest worker (``email_weekly_digest_worker``) ships
-- exactly one Sunday-evening recap to the single configured ``smtp_to``
-- address: a great default but useless when the operator wants to mail
-- only a *slice* of activity (e.g. every shot tagged ``#work-decisions``)
-- to a *different* recipient — a teammate, a personal archive address,
-- a second inbox. This table is the persisted subscription list that
-- lets one Persona instance fan out N tag-scoped digests.
--
-- Schema
-- ------
--   * ``id``           — surrogate key. The same (tag, email) pair is
--                        ``UNIQUE`` so the settings UI's "save" is a
--                        true upsert (re-submitting the same pair just
--                        updates the day/hour), but ``id`` is what the
--                        delete + send-now endpoints route on.
--   * ``tag``          — raw tag name (case-insensitive match against
--                        ``tags.name`` at digest-build time, mirroring
--                        :mod:`app.tag_feed`). Not foreign-keyed because
--                        a subscription that outlives every shot in its
--                        tag should still be editable / deletable; the
--                        digest body simply renders "no shots this week"
--                        when the JOIN comes back empty.
--   * ``email``        — destination address. Multiple subscriptions can
--                        share an address (different tags → same inbox);
--                        different addresses can share a tag (one bucket
--                        → many recipients). The ``UNIQUE(tag, email)``
--                        pair is the only enforced constraint.
--   * ``day_of_week``  — 0..6 (Mon..Sun) — when the per-tag digest fires.
--                        Default ``6`` (Sunday) matches the global weekly
--                        digest cadence so a fresh subscription lines up
--                        with the rest of the user's recap rhythm.
--   * ``hour_local``   — 0..23 — local-clock hour to fire on. Default
--                        ``19`` matches the global weekly digest default.
--   * ``enabled``      — ``1`` = active, ``0`` = paused. Same boolean
--                        convention every other opt-in feature in the
--                        project uses, so the UI checkbox maps 1:1.
--   * ``created_at``   — ISO-8601 UTC of the row's insert; useful for
--                        audit + "this is a new sub, never sent" UX.
--   * ``last_sent_at`` — ISO-8601 UTC of the most recent successful send.
--                        ``NULL`` means "never sent". The worker's
--                        due-ness check filters on this column with a
--                        6-day floor so a sub never fires twice in the
--                        same week even if the operator nudges the
--                        clock backwards or restarts the daemon at the
--                        firing hour.
--
-- Indexes
-- -------
-- The hot read path is the worker's hourly "what's due?" sweep:
-- ``WHERE enabled = 1 AND day_of_week = ? AND hour_local = ?`` — covered
-- by the composite ``idx_tag_email_sub_due`` so the lookup stays a
-- bounded index range even on installs with hundreds of subscriptions.
--
-- ``CREATE TABLE IF NOT EXISTS`` keeps this idempotent on re-runs
-- (:func:`app.storage.db.init_database` replays every ``*.sql`` in
-- lexicographic order on every boot).

CREATE TABLE IF NOT EXISTS tag_email_subscription (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tag TEXT NOT NULL,
    email TEXT NOT NULL,
    day_of_week INTEGER NOT NULL DEFAULT 6
        CHECK (day_of_week BETWEEN 0 AND 6),
    hour_local INTEGER NOT NULL DEFAULT 19,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_sent_at TEXT,
    UNIQUE(tag, email)
);

CREATE INDEX IF NOT EXISTS idx_tag_email_sub_due
    ON tag_email_subscription(enabled, day_of_week, hour_local);
