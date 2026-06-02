-- v0.31 — SMTP delivery for daily/weekly LLM digests.
--
-- Reuses the existing ``kv_settings`` (key/value/updated_at) table from
-- schema.sql instead of introducing a dedicated SMTP table: the surface
-- is tiny (eight strings) and storing them as kv rows keeps the GET/POST
-- settings route trivial.
--
-- All values are TEXT — booleans are persisted as the literal strings
-- ``'true'`` / ``'false'`` so the kv layer stays type-free. The opt-in
-- gate is ``smtp_enabled``; until the user flips it to ``'true'`` the
-- ``send_digest_email`` helper short-circuits with status ``disabled``
-- regardless of the other rows.
--
-- ``INSERT OR IGNORE`` only seeds rows the user has never written, so
-- re-running this migration after a real save never clobbers settings.

INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('smtp_host', '');
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('smtp_port', '587');
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('smtp_user', '');
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('smtp_pass', '');
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('smtp_to', '');
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('smtp_from', '');
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('smtp_tls', 'true');
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('smtp_enabled', 'false');
