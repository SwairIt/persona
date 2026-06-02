-- v0.45 — per-app icon cache.
--
-- One row per ``app_name`` (the Win32 window-class / executable name that
-- the capture loop already records on every screenshot — e.g. ``Slack``,
-- ``chrome.exe``, ``devenv.exe``). The PNG blob is the rendered 64×64 icon
-- served at ``/app-icon/{app_name}.png`` with a long ``Cache-Control``
-- max-age. We keep the image inline rather than on the filesystem so the
-- cache is portable with the SQLite file (backups, exports, sync) and so
-- a wipe is a single DELETE rather than a directory walk.
--
-- ``source`` records how the icon was obtained so we can audit / refresh
-- selectively without losing the human-derived ones:
--   * ``shell32`` — extracted from a running process's exe via the
--     Win32 ``ExtractIconExW`` path (best quality, Windows-only).
--   * ``initials`` — deterministic fallback: a 64×64 PNG with the first
--     two letters of ``app_name`` on a hue derived from a stable hash.
--
-- ``generated_at`` lets a future maintenance job re-extract icons older
-- than N days if the user upgrades the app (icon changes between
-- versions). Today we never invalidate automatically — callers go through
-- :func:`app.app_icons.invalidate`.

CREATE TABLE IF NOT EXISTS app_icon (
    app_name TEXT PRIMARY KEY,
    png_bytes BLOB NOT NULL,
    source TEXT NOT NULL,
    generated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
