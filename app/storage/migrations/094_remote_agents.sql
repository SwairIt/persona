-- v1.12 feature 1/3 — server-side endpoints for remote capture agents.
--
-- A "remote agent" is a small uploader running on a different machine
-- (Mac, mobile device, secondary laptop) that pushes audio segments and
-- screenshots to this Persona instance over HTTPS. The agent
-- authenticates with a bearer token; the raw value is shown to the
-- operator exactly once when the agent row is created and only the
-- SHA-256 digest is persisted here.
--
-- Column contract
-- ---------------
--   * ``name`` — human-readable label (e.g. ``"macbook-air"``); shown in
--     the admin UI and stamped into every audit_log row that the agent
--     triggers indirectly.
--   * ``token_hash`` — SHA-256 hex of the raw bearer token. UNIQUE so a
--     re-presented token always lands on the same row; never stores the
--     raw value (lost token → revoke + re-issue).
--   * ``platform`` — free-form tag the operator picks from the create
--     form (``"macos"`` / ``"ios"`` / ``"linux"`` / ``"other"``). NULL
--     allowed so older rows survive without backfill.
--   * ``last_seen_at`` — most recent successful authenticated request
--     (any endpoint, including ``/heartbeat``). The dashboard sorts on
--     this column.
--   * ``last_audio_at`` / ``last_screen_at`` — narrower liveness probes
--     so an operator can see *which* upload paths the agent is actually
--     using. Updated by the audio-segment / screenshot routes only.
--   * ``revoked_at`` — soft revoke. The row stays in the table so the
--     audit trail survives; verify_agent_token treats a non-NULL value
--     as "reject the token even if the hash matches".
--
-- v1.12 also extends ``screenshots`` with a ``source`` column so a
-- screenshot uploaded by a remote agent is distinguishable from a local
-- capture (``"local"`` / ``"remote_agent"`` / ``"manual"`` /
-- ``"import"``). The default keeps every pre-existing row tagged as
-- ``"local"`` so existing queries keep behaving like before.

CREATE TABLE IF NOT EXISTS remote_agent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    platform TEXT,
    last_seen_at TEXT,
    last_audio_at TEXT,
    last_screen_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_remote_agent_token_hash
    ON remote_agent (token_hash);

-- v1.12 — distinguish remote-agent uploads from local captures. NULL on
-- legacy rows; the read path coerces NULL → "local" so dashboards do not
-- need to special-case it.
ALTER TABLE screenshots ADD COLUMN source TEXT;
