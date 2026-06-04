-- v1.37 — dashboard grid editor: per-slot widget order + visibility.
--
-- The earlier v0.81 ``dashboard_tile_csv`` (a single kv row) and v0.86
-- ``dashboard_widget`` table (saved-FTS-query tiles) both live for
-- their own reasons — this migration adds a *third* surface that
-- targets the *built-in* hand-coded widgets shipped since v1.35
-- (today_vs_average, voice_note, capture_status, …). Users want a
-- drag-drop editor for those, with explicit position + enabled flags
-- rather than the CSV-in-kv hack.
--
-- The table is named ``dashboard_grid_slot`` rather than the spec's
-- ``dashboard_widget`` because the latter already exists with a
-- completely different schema (id/title/query/position/created_at for
-- saved-search tiles) — re-using the name would fail at migration time
-- on every existing install. The route + service layer expose the new
-- table under the public ``dashboard widgets`` UX label; the storage
-- name is private and explicit about its grid-slot semantics.
--
-- Columns:
--   slot_id     — PK, autoincrement so reordering never has to mutate
--                 identity. The dashboard renderer never serialises
--                 this; it is purely a stable join key for upserts.
--   widget_key  — opaque identifier from
--                 :data:`app.dashboard_widgets.WIDGET_CATALOGUE`.
--                 NOT a FK because the catalogue is code-defined; the
--                 service layer drops rows whose key has been removed.
--   position    — 0-based render order. Index below keeps the
--                 hot-path ``ORDER BY position`` cheap even when the
--                 user has every catalogue widget enabled.
--   enabled     — 0/1 visibility flag. Separate index because the
--                 renderer's query is ``WHERE enabled = 1 ORDER BY
--                 position`` — a partial-style filter that benefits
--                 from a dedicated index when half the catalogue is
--                 hidden.
--   options_json — reserved for per-widget configuration (e.g. the
--                  voice_note widget's recording length, the
--                  budget_meter widget's threshold). Nullable so the
--                  seven seeded widgets ship without forcing the user
--                  through a form on first boot.
--   updated_at  — last write, ISO-8601 UTC via SQLite ``datetime('now')``.

CREATE TABLE IF NOT EXISTS dashboard_grid_slot (
    slot_id INTEGER PRIMARY KEY AUTOINCREMENT,
    widget_key TEXT NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    options_json TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_dashboard_grid_slot_position
    ON dashboard_grid_slot (position);

CREATE INDEX IF NOT EXISTS idx_dashboard_grid_slot_enabled
    ON dashboard_grid_slot (enabled);

-- Seed the current /dashboard widget order so users see their existing
-- layout reflected on first visit to /settings/dashboard-grid. The
-- INSERT … SELECT WHERE NOT EXISTS guard makes the migration idempotent
-- without requiring an UPSERT — re-running it on an already-seeded
-- database is a no-op.
INSERT INTO dashboard_grid_slot (widget_key, position, enabled)
SELECT 'today_vs_average', 0, 1
WHERE NOT EXISTS (SELECT 1 FROM dashboard_grid_slot WHERE widget_key = 'today_vs_average');

INSERT INTO dashboard_grid_slot (widget_key, position, enabled)
SELECT 'capture_status', 1, 1
WHERE NOT EXISTS (SELECT 1 FROM dashboard_grid_slot WHERE widget_key = 'capture_status');

INSERT INTO dashboard_grid_slot (widget_key, position, enabled)
SELECT 'latest_digest', 2, 1
WHERE NOT EXISTS (SELECT 1 FROM dashboard_grid_slot WHERE widget_key = 'latest_digest');

INSERT INTO dashboard_grid_slot (widget_key, position, enabled)
SELECT 'voice_note', 3, 1
WHERE NOT EXISTS (SELECT 1 FROM dashboard_grid_slot WHERE widget_key = 'voice_note');

INSERT INTO dashboard_grid_slot (widget_key, position, enabled)
SELECT 'top_apps_7d', 4, 1
WHERE NOT EXISTS (SELECT 1 FROM dashboard_grid_slot WHERE widget_key = 'top_apps_7d');

INSERT INTO dashboard_grid_slot (widget_key, position, enabled)
SELECT 'streak_card', 5, 1
WHERE NOT EXISTS (SELECT 1 FROM dashboard_grid_slot WHERE widget_key = 'streak_card');

INSERT INTO dashboard_grid_slot (widget_key, position, enabled)
SELECT 'budget_meter', 6, 1
WHERE NOT EXISTS (SELECT 1 FROM dashboard_grid_slot WHERE widget_key = 'budget_meter');
