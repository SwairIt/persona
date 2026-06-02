-- v0.29: configurable Tesseract language list.
-- Seeds a default ``ocr_languages`` row in ``kv_settings`` so the UI has
-- something to render on first boot. The actual table already exists
-- (see ``schema.sql`` / migration 001) so this migration only inserts a
-- seed value if the user has not configured one yet.

INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('ocr_languages', 'eng');
