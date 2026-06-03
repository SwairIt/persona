-- v0.85 — per-feed bearer tokens for RSS / Atom feeds.
--
-- Background
-- ----------
-- Persona's ``/feeds/*`` endpoints have always been openly readable to
-- anyone who can reach the host. That was fine while feeds were
-- loopback-only, but v0.85 makes the host shareable: a user wants to
-- send a friend the URL of *one* tag feed without also handing them
-- ``/feeds/journal.rss``. This table is the back-end for the new
-- "Feed tokens" settings page.
--
-- A row in ``feed_token`` represents a single shareable token that is
-- valid for the feed paths matching ``feed_pattern`` (an ``fnmatch``
-- glob — e.g. ``/feeds/tags/*.rss`` or ``/feeds/journal.rss``). The
-- raw token value is never stored: callers only ever see its SHA-256
-- hex digest. Recovery from a lost token is "revoke + re-issue",
-- never "decrypt" — same contract as the v0.34 API-token table.
--
-- Enforcement is gated on the new ``feed_auth_required`` setting
-- (default False). When the setting is off the RSS routes keep their
-- legacy open-access behaviour; only when the operator opts in does
-- ``?token=…`` become mandatory. That way an upgrade-in-place doesn't
-- break anyone's existing feed-reader subscriptions until they're
-- ready to flip the switch.
--
-- Columns
-- -------
--   * ``name``          — operator-visible label, never the secret.
--   * ``token_hash``    — SHA-256 hex digest of the raw urlsafe token.
--                         ``UNIQUE`` so verification can use an indexed
--                         equality lookup with no chance of collision.
--   * ``feed_pattern``  — fnmatch glob the request path must match.
--                         Stored verbatim (e.g. ``/feeds/tags/*.rss``);
--                         matching happens in Python via ``fnmatch``.
--   * ``revoked_at``    — kill switch. ``verify_token`` rejects rows
--                         where this is non-NULL even if the hash and
--                         pattern both match. We never DELETE rows so
--                         the audit trail (name / created_at) survives.
--
-- ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` keep
-- the migration idempotent across re-runs of ``init_database``.

CREATE TABLE IF NOT EXISTS feed_token (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    feed_pattern TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_feed_token_hash ON feed_token(token_hash);
