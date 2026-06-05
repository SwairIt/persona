-- v1.62 — Nightly CSV-to-webhook pipeline destinations.
--
-- Each row is one external system that wants a periodic CSV dump POSTed
-- to it. Compare with ``webhooks`` (migration ``055``) — that table
-- carries *event* webhooks (one HTTP call per shot/note created); this
-- one carries *batch* webhooks (one HTTP call per N-day window dump of
-- a chosen ``csv_kind``). Different cadence, different payload shape,
-- different table.
--
-- Schema
-- ------
--   * ``id``              — surrogate key. The settings UI routes delete
--                            + send-now on this column.
--   * ``name``            — human-friendly slug shown in the UI ("Notion
--                            DB", "Sheets backup"). ``UNIQUE`` so the
--                            new-row form behaves as a true upsert.
--   * ``webhook_url``     — full destination URL. Treated as a secret:
--                            never logged, never echoed in error
--                            messages, because the URL itself can carry
--                            an auth token in the query string (Zapier,
--                            Notion proxies, GAS triggers all do this).
--   * ``csv_kind``        — which of the four big-data streams to dump.
--                            ``CHECK`` enforces the closed set so a
--                            typo never silently posts an empty body.
--                            The four kinds match the public helpers in
--                            :mod:`app.csv_export`.
--   * ``days_window``     — how many days of history to include in each
--                            dump. ``1`` (default) is "yesterday only"
--                            — the typical nightly incremental for a
--                            Notion / Sheets sync. ``7`` would be a
--                            weekly rollup. Any positive int is valid;
--                            the worker subtracts it from "today" to
--                            build the ``date_from`` bound.
--   * ``enabled``         — ``1`` = active, ``0`` = paused. The same
--                            convention every other opt-in feature uses.
--   * ``hour_local``      — 0..23. Local-clock hour the worker fires
--                            on. Default ``5`` (early morning) so the
--                            dump lands before the operator's working
--                            day starts; the destination sync is fresh
--                            when they sit down.
--   * ``headers_json``    — optional JSON object of custom request
--                            headers. ``NULL`` means "just Content-Type:
--                            text/csv". Lets the operator wire bearer
--                            tokens, Notion's ``Notion-Version`` pin,
--                            or a Zapier secret without re-deploying.
--   * ``created_at``      — ISO-8601 UTC of the insert. Audit + "this
--                            is a brand-new destination, never fired".
--   * ``last_sent_at``    — ISO-8601 UTC of the last attempt (not just
--                            success — the operator wants to see "we
--                            tried at 05:00 and got a 500" without
--                            grepping the log).
--   * ``last_status_code`` — last HTTP status code observed. ``NULL``
--                            until the first fire. Surfaced in the UI
--                            so a 2xx vs 5xx is one glance away.
--   * ``last_error``      — last error string when the call did not
--                            even produce a status code (DNS fail,
--                            socket reset, TLS error). ``NULL`` on a
--                            clean attempt — even a 5xx clears this.
--
-- Why no foreign keys
-- -------------------
-- ``csv_kind`` is enforced by the ``CHECK`` rather than a lookup table
-- because the four kinds are a closed code-level constant (they map
-- 1:1 to the four ``stream_*`` helpers in :mod:`app.csv_export`); a
-- table would be more flexible but every read would JOIN against four
-- rows that never change.
--
-- ``CREATE TABLE IF NOT EXISTS`` keeps this idempotent on re-runs.

CREATE TABLE IF NOT EXISTS webhook_csv_destination (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    webhook_url TEXT NOT NULL,
    csv_kind TEXT NOT NULL
        CHECK (csv_kind IN ('screenshots', 'notes', 'hourly_cards', 'audio_segments')),
    days_window INTEGER NOT NULL DEFAULT 1,
    enabled INTEGER NOT NULL DEFAULT 1,
    hour_local INTEGER NOT NULL DEFAULT 5,
    headers_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_sent_at TEXT,
    last_status_code INTEGER,
    last_error TEXT
);

CREATE INDEX IF NOT EXISTS idx_webhook_csv_destination_due
    ON webhook_csv_destination(enabled, hour_local);
