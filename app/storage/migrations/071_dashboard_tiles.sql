-- v0.81 — customisable dashboard tile order persisted in kv_settings.
--
-- Mirrors the storage pattern that 070_grayscale.sql / 061_compact_mode.sql
-- established: a single string-typed row in ``kv_settings`` keyed
-- ``'dashboard_tiles'``. The value is a comma-separated list of tile
-- identifiers in the order the user wants them rendered on /dashboard
-- (v0.65). Tiles omitted from the CSV are hidden; tiles present in an
-- unknown position keep their relative order.
--
-- The route layer (``app.web.routes.dashboard_tiles``) parses the CSV
-- against a server-side whitelist so a manual kv edit can never inject
-- an unknown tile name into the dashboard renderer — unknown entries
-- are dropped silently and the default ordering fills in any missing
-- tiles.
--
-- ``INSERT OR IGNORE`` only seeds the row when it is missing so re-runs
-- of the migration after the user reorders their tiles never reset the
-- choice. The default value lists every shipping tile in the historical
-- order ``dashboard.html`` rendered them before v0.81 — so an upgrade
-- is a no-op visually until the user opens /settings/dashboard.

INSERT OR IGNORE INTO kv_settings (key, value)
VALUES ('dashboard_tiles', 'today,streak,top_apps,latest_digest,capture_status,pinned');
