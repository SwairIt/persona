-- v0.60 — display-name aliases for ``app_name``.
--
-- The capture loop persists the Win32 executable / window-class string
-- verbatim on every screenshot row (``devenv.exe``, ``Code.exe``,
-- ``chrome.exe``). Those strings are stable identifiers — perfect as a
-- grouping key — but ugly in the UI: a user wants to see "Visual Studio"
-- on the timeline, not "devenv.exe".
--
-- This table maps the raw ``original_name`` (exactly as it appears in
-- ``screenshots.app_name``) to a human-friendly ``display_name``. The
-- lookup is consulted by the :func:`app.app_aliases.resolve` helper that
-- the Jinja ``app_alias`` filter delegates to — a single round-trip per
-- render covers every cell in the timeline / time-on-app table because
-- the helper caches in-process.
--
-- We intentionally keep the storage primitive small (two columns, one
-- PK): renaming is a per-string overlay, not a workflow with versioning,
-- timestamps, or per-user scope. If a future release needs any of those
-- they go in a follow-up table — bolting them on here would force every
-- caller to thread metadata it doesn't need.

CREATE TABLE IF NOT EXISTS app_alias (
    original_name TEXT PRIMARY KEY,
    display_name TEXT NOT NULL
);
