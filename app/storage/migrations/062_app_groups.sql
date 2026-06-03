-- v0.63 — app → group assignment for category-level stats.
--
-- Every screenshot row already carries an ``app_name`` (raw Win32
-- executable / window class — ``devenv.exe``, ``Code.exe``,
-- ``chrome.exe``). The 060_app_aliases.sql overlay lets the user rename
-- those for display; this table sits beside it and answers a different
-- question: *which bucket does the app belong to?*
--
-- Group names are free-form text (``work``, ``personal``, ``comms``,
-- ``dev``, ``games`` — but the UI is happy to accept anything the user
-- types). The contract is one group per app: the primary key on
-- ``app_name`` makes the upsert pattern in ``set_group()`` trivial and
-- prevents an app from leaking into two buckets at once. To remove an
-- app from its group, the helper layer deletes the row entirely — there
-- is no "ungrouped" sentinel, the absence of a row IS the absence of a
-- group.
--
-- Like 060_app_aliases, we deliberately keep the schema primitive: no
-- per-user scope, no timestamps, no soft-delete. Group membership is a
-- single-string overlay; anything richer (per-day attribution, history)
-- goes in a follow-up table so callers that just want "which group is
-- chrome.exe in?" pay one round-trip with no joins.

CREATE TABLE IF NOT EXISTS app_group (
    app_name TEXT PRIMARY KEY,
    group_name TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_app_group_name ON app_group (group_name);
