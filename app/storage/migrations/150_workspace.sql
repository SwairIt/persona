-- v1.64 — Workspace contexts: single-click holistic switch.
--
-- A *workspace* is the next generalisation of v1.49's ``focus_profile``:
-- instead of bundling just the capture-loop knobs (interval / audio /
-- meeting-pause / theme), it also pulls in the v1.32 blocklist, the
-- v1.49 focus profile itself (as a FK), and a default filter applied
-- to the active-window timeline view. Activating one workspace flips
-- five subsystems at once in a single transaction:
--
--   theme                            — light / dark / auto kv row
--   capture_interval_seconds_live    — capture-loop cadence kv row
--   focus_profile_id                 — chained activation through
--                                      app.focus_profiles.activate_profile
--   blocklist_apps_json              — JSON array of app names; the
--                                      capture-blocklist UI reads this
--                                      lazily so we keep storage shape
--                                      forward-compatible
--   default_timeline_filter          — opaque text the timeline view
--                                      uses as a query-string default
--                                      (e.g. ``app=VSCode`` or
--                                      ``mode=focus``)
--
-- The active flag is enforced at the helper layer the same way
-- ``focus_profile`` does it — UPDATE-zero-out + UPDATE-set-one in one
-- transaction — and the partial index over ``is_active = 1`` makes the
-- "which workspace is on?" lookup constant time.
--
-- Schema notes
-- ------------
--   name             — operator-facing label, UNIQUE so the
--                      install_preset helper can use INSERT OR IGNORE
--                      for idempotency. Three presets ship out of the
--                      box (Coder, Writer, Reader) — see
--                      ``app.workspaces.PRESET_WORKSPACES``.
--   description      — optional human-readable hint rendered on the
--                      card on /workspaces.
--   theme            — one of dark/light/auto; NULL means "do not
--                      touch the kv row when activating".
--   capture_interval_seconds
--                    — REAL because the kv row stores it as a
--                      stringified float; NULL means "do not touch".
--   focus_profile_id — optional FK; activating the workspace chains
--                      through to ``app.focus_profiles.activate_profile``
--                      so the operator gets both the workspace bundle
--                      AND the existing focus-profile bundle in one
--                      click. ON DELETE SET NULL so removing the
--                      underlying profile does not orphan the row
--                      (the workspace simply becomes "profile-less"
--                      and stops chaining the focus activation).
--   blocklist_apps_json
--                    — JSON-encoded list of app names. TEXT for
--                      forward-compat with sqlite; the helper layer
--                      parses + validates. Reserved for the future
--                      capture-blocklist integration; nothing reads
--                      it yet beyond echoing it back in the JSON API.
--   default_timeline_filter
--                    — opaque text the active-window timeline view
--                      can use as its default ?filter= query string.
--                      NULL means "no default".
--   is_active        — exactly one row may carry 1; the
--                      activate_workspace helper enforces this in a
--                      transaction. The partial index below points at
--                      that row directly.
--   created_at       — ISO timestamp, default datetime('now'). Matches
--                      every other v1.4x / v1.5x / v1.6x table.

CREATE TABLE IF NOT EXISTS workspace (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    theme TEXT,
    capture_interval_seconds REAL,
    focus_profile_id INTEGER REFERENCES focus_profile(id) ON DELETE SET NULL,
    blocklist_apps_json TEXT,
    default_timeline_filter TEXT,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_workspace_active
    ON workspace(is_active) WHERE is_active = 1;
