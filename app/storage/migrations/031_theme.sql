-- v0.32 — UI theme (dark / light / auto) persisted in kv_settings.
--
-- Reuses the existing ``kv_settings`` key/value table the same way the
-- SMTP and OCR-language migrations do: a single row keyed ``'theme'``
-- with a textual value the route validates against the three-element
-- whitelist (``dark`` / ``light`` / ``auto``). No dedicated table because
-- the surface is one string and the GET/POST handler stays trivial.
--
-- ``INSERT OR IGNORE`` only seeds the row when it does not exist, so
-- re-running the migration after the user picks a different theme never
-- clobbers their choice. Default is ``dark`` to preserve the historical
-- look of ``base.html`` (which shipped with ``class="dark"`` hard-coded
-- prior to v0.32).

INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('theme', 'dark');
