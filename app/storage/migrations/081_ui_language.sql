-- v1.1 feature 1/3 — UI language selector persisted in kv_settings.
--
-- Mirrors the storage pattern established by 077_anim_toggle.sql and
-- 070_grayscale.sql: a single string-typed row in ``kv_settings`` keyed
-- ``'ui_language'``. The write/read paths normalise to a value in the
-- :data:`app.i18n.SUPPORTED_LANGUAGES` whitelist (currently ``"en"`` /
-- ``"ru"``). Anything outside the whitelist collapses to the default
-- ``"en"`` on read so a manual kv edit cannot wedge the renderer.
--
-- The setting drives the Jinja ``t(key)`` global wired up in
-- :mod:`app.web.templates_engine` and exposed via the language selector
-- in :mod:`app.web.routes.settings`.
--
-- ``INSERT OR IGNORE`` only seeds the row when it is missing so re-runs
-- of the migration after the user picks Russian never reset their
-- choice. Default is ``"en"`` to preserve the historical (English-only)
-- UI for every existing install — opt-in localisation, not opt-out.

INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('ui_language', 'en');
