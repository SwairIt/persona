-- v1.28 — outbox templates for Linear / Notion / Slack / generic fan-out.
--
-- Persona already ships a generic outbound-webhook subscription system
-- (``webhook_subscription`` + ``webhook_retry_queue``) where the operator
-- supplies a raw URL and the dispatcher signs+POSTs the Persona-shaped
-- envelope. That works for receivers under the operator's control but is
-- useless for third-party SaaS — Linear's GraphQL API and Notion's REST
-- API expect their *own* request shapes (specific endpoints, bearer
-- tokens, JSON schemas).
--
-- ``outbox_template`` adds a parallel, format-aware channel: one row per
-- (service, event_kind, body template). When an event fires the
-- :mod:`app.outbox` dispatcher renders ``body_template`` against the
-- event's payload via ``str.format`` and POSTs the result with the
-- per-row ``auth_header`` (e.g. ``Bearer <api-token>``) instead of the
-- Persona signature. The two systems coexist deliberately — the
-- signed-envelope path is for receivers that trust Persona's wire
-- format, this one is for receivers that don't.
--
-- Columns:
--   name          — operator-facing label (e.g. "Linear inbox comment").
--   service       — controlled vocabulary so the admin UI can show the
--                   right help-text per receiver. ``generic`` is an
--                   escape hatch for receivers we haven't hand-baked.
--   event_kind    — see ``app/outbox.py`` module docstring for the
--                   active list. Free-form text intentionally; adding a
--                   new event_kind is a code change but does not require
--                   a migration.
--   target_url    — full URL the dispatcher POSTs to.
--   auth_header   — verbatim value for the ``Authorization`` header
--                   (e.g. ``Bearer secret_xxx``). Nullable for receivers
--                   that authenticate via querystring or no auth at all.
--   body_template — ``str.format``-style template. Available placeholders
--                   are whatever the caller passes into ``payload``.
--                   Stored as TEXT so JSON, GraphQL queries and form
--                   bodies all fit.
--   enabled       — soft toggle so the operator can pause a template
--                   without deleting + re-entering the body.
--   created_at    — ISO-8601 UTC, SQLite ``datetime('now')``.

CREATE TABLE IF NOT EXISTS outbox_template (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    service TEXT NOT NULL CHECK (service IN ('linear', 'notion', 'slack', 'generic')),
    event_kind TEXT NOT NULL,
    target_url TEXT NOT NULL,
    auth_header TEXT,
    body_template TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The dispatcher's hot-path query is
-- ``WHERE event_kind = ? AND enabled = 1`` — covered by this composite
-- index so even an operator with hundreds of templates resolves the
-- fan-out list in a single index range scan.
CREATE INDEX IF NOT EXISTS idx_outbox_template_event_enabled
    ON outbox_template (event_kind, enabled);
