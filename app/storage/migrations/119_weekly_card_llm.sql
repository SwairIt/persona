-- v1.27 — opt-in LLM weekly rollup column.
--
-- Adds two nullable columns to ``weekly_card`` so the optional weekly
-- rollup worker can persist a single-paragraph LLM narrative built
-- from the week's 7 daily_pin rows + 168 hourly_card rows. NULL is the
-- "not yet generated" sentinel and is the default state for every
-- existing row; the worker only fills it in when the operator has
-- toggled ``kv_settings.weekly_llm_rollup_enabled = '1'``.
--
-- Stored on the existing tier-2 row rather than a sidecar table so the
-- ``/memory/weeks`` view can render the paragraph alongside the
-- heuristic summary without an extra join.

ALTER TABLE weekly_card ADD COLUMN llm_summary TEXT;
ALTER TABLE weekly_card ADD COLUMN llm_generated_at TEXT;
