-- v1.47 — Focus-session app whitelist (inverse of focus_blocklist).
--
-- The v0.85 ``focus_blocklist`` is a denylist: rows there are *skipped*
-- while a focus_session is active. That is the right knob when the user
-- knows the small set of apps that derail them (Slack, Telegram, news).
-- It is the wrong knob when the user wants the opposite framing — a
-- short *allowlist* of the apps that belong to the current deep-work
-- block (the IDE, Figma, the spec PDF) and everything else should be
-- treated as a distraction.
--
-- This table is that allowlist. It is consulted by the capture loop
-- *only* while a focus_session is active. Semantics:
--
--   * empty whitelist  → open mode; nothing extra is blocked. The
--                        existing focus_blocklist still applies.
--   * non-empty list   → the capture loop skips any shot whose
--                        ``active_window.app_name`` is NOT on the list.
--                        The blocklist still applies on top, so an app
--                        can be both implicitly allowed by the empty-
--                        list rule and explicitly blocked elsewhere.
--
-- Outside an active focus_session this list has no effect — same
-- conditional-block contract as ``focus_blocklist``.
--
-- Schema notes:
--   id        — autoincrement so the admin UI's delete-by-id form has a
--               stable key. Mirrors ``ai_reminder``/``capture_regex_blocklist``
--               rather than the older ``focus_blocklist`` shape (which
--               keyed by ``app_name``) because operators have asked for
--               a delete button that survives renames.
--   app_name  — raw, UNIQUE. Normalisation (strip + casefold) is done at
--               the helper layer in ``app.focus_whitelist`` so the
--               operator never sees ``"slack"`` and ``"Slack"`` as two
--               separate rows.
--   added_at  — ISO timestamp, default ``datetime('now')``.

CREATE TABLE IF NOT EXISTS focus_whitelist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_name TEXT NOT NULL UNIQUE,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);
