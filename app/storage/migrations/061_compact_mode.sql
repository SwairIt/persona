-- v0.61 — global compact mode toggle persisted in kv_settings.
--
-- Mirrors the storage pattern that 031_theme.sql introduced: a single
-- string-typed row in ``kv_settings`` keyed ``'compact_mode'``. The
-- write/read paths normalise to ``"1"`` (on) or ``"0"`` (off) — the
-- ``base.html`` template stamps the resulting value onto the ``<body>``
-- as a ``data-compact`` attribute, and ``compact_mode.css`` selects on
-- ``[data-compact="1"]`` to shrink margins, font-size, and thumbnail
-- variables.
--
-- ``INSERT OR IGNORE`` only seeds the row when it is missing so re-runs
-- of the migration after the user opts in never reset their choice.
-- Default is ``"0"`` to preserve the historical (roomy) layout for
-- every existing install.

INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('compact_mode', '0');
