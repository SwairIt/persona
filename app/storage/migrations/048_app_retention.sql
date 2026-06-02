-- v0.49 — per-app retention overrides.
--
-- Global retention is a single dial: ``tier_warm_after_days``,
-- ``tier_cold_after_days``, and the soft ``retention_days`` ceiling all
-- apply uniformly. In practice some apps deserve different policies —
-- VS Code source-of-truth screenshots may want to live forever, Slack
-- chatter can demote and disappear faster than the global default.
--
-- This table stores a sparse, per-``app_name`` override. Each numeric
-- column is NULLable so the operator can override one knob (e.g. only
-- ``cold_after_days``) and inherit the rest from the global settings.
-- ``never_delete`` is a hard switch that the retention worker honours
-- by skipping the row entirely from both the warm-demote and the cold-
-- demote passes.
--
-- ``app_name`` is the primary key and matches verbatim the value the
-- capture loop persists on ``screenshots.app_name`` (case-sensitive),
-- the same shape used by :mod:`app.storage.app_overrides` for capture-
-- interval overrides. Lookups in the worker are by exact equality so
-- no normalisation is required here.

CREATE TABLE IF NOT EXISTS app_retention (
    app_name TEXT PRIMARY KEY,
    warm_after_days INTEGER,
    cold_after_days INTEGER,
    delete_after_days INTEGER,
    never_delete INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
