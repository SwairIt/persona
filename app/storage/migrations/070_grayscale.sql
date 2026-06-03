-- v0.78 — global grayscale toggle persisted in kv_settings.
--
-- Mirrors the storage pattern established by 061_compact_mode.sql: a
-- single string-typed row in ``kv_settings`` keyed ``'grayscale_mode'``.
-- The write/read paths normalise to ``"1"`` (on) or ``"0"`` (off) — the
-- ``base.html`` template stamps the resulting value onto the ``<body>``
-- as a ``data-grayscale`` attribute, and ``grayscale.css`` selects on
-- ``body[data-grayscale="1"]`` to wrap the whole document in
-- ``filter: grayscale(1)`` for distraction-light reading.
--
-- ``INSERT OR IGNORE`` only seeds the row when it is missing so re-runs
-- of the migration after the user opts in never reset their choice.
-- Default is ``"0"`` to preserve the historical (full-colour) layout
-- for every existing install.

INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('grayscale_mode', '0');
