-- v1.2 feature 1/3 — customisable keyboard shortcuts persisted in kv_settings.
--
-- Mirrors the storage pattern established by 081_ui_language.sql and
-- 077_anim_toggle.sql: a single string-typed row in ``kv_settings`` keyed
-- ``'kbd_shortcuts_json'``. The value is a JSON object mapping *action
-- name* → *key string*. The renderer in
-- :mod:`app.web.routes.kbd_shortcuts` validates the JSON on read; a
-- malformed kv edit collapses to the default map rather than wedging the
-- editor or the keyboard listeners.
--
-- The actions covered are the four user-facing shortcuts wired up across
-- :file:`keyboard_shortcuts.js`, :file:`search_keyboard.js`,
-- :file:`quick_pin.js`, and :file:`image_viewer.js`:
--
--   help_overlay   → ``"?"``  open the shortcuts cheatsheet
--   search_focus   → ``"/"``  focus the search input
--   go_timeline    → ``"g t"`` multi-key sequence: go to /timeline
--   pin_toggle     → ``"p"``  toggle pin on the active shot
--   fullscreen     → ``"f"``  fullscreen the first zoomable image
--
-- The default JSON below is exactly what the existing hard-coded
-- listeners ship with, so a fresh install behaves identically to every
-- previous version — opt-in customisation, not a behaviour change.
--
-- ``INSERT OR IGNORE`` only seeds the row when it is missing so re-runs
-- of the migration after the user has rebound their shortcuts never
-- reset their choices.

INSERT OR IGNORE INTO kv_settings (key, value) VALUES (
    'kbd_shortcuts_json',
    '{"help_overlay":"?","search_focus":"/","go_timeline":"g t","pin_toggle":"p","fullscreen":"f"}'
);
