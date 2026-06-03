-- v0.93 — global reduce-motion toggle persisted in kv_settings.
--
-- Mirrors the storage pattern established by 070_grayscale.sql and
-- 061_compact_mode.sql: a single string-typed row in ``kv_settings``
-- keyed ``'reduce_motion'``. The write/read paths normalise to ``"1"``
-- (motion suppressed) or ``"0"`` (motion allowed) — the ``base.html``
-- template stamps the resulting value onto the ``<body>`` as a
-- ``data-reduce-motion`` attribute, and ``reduce_motion.css`` selects
-- on ``body[data-reduce-motion="1"]`` to disable every CSS transition
-- and animation globally for users sensitive to motion (vestibular
-- disorders, post-concussion, plain preference).
--
-- ``INSERT OR IGNORE`` only seeds the row when it is missing so re-runs
-- of the migration after the user opts in never reset their choice.
-- Default is ``"0"`` to preserve the historical (animated) behaviour
-- for every existing install — opt-in, not opt-out, because most users
-- expect motion and would be confused by a flat-static UI on upgrade.

INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('reduce_motion', '0');
