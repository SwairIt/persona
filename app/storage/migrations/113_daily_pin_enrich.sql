-- v1.19 — opt-in LLM enrichment for tier-5 daily pins.
--
-- Mirrors the hourly_card.llm_enriched / .llm_narrative pattern added in
-- migration 097: the deterministic daily_pin row is written first by
-- daily_pin_worker, then an opt-in second pass (gated by
-- kv_settings.daily_pin_llm_enrichment_enabled = '1') asks the
-- configured LLM provider for ONE short narrative paragraph and stores
-- it alongside the heuristic pin without touching the original `pin`
-- column. The flag flips to 1 once enrichment succeeds so the worker
-- never doubles up.

ALTER TABLE daily_pin ADD COLUMN llm_narrative TEXT;
ALTER TABLE daily_pin ADD COLUMN llm_enriched INTEGER NOT NULL DEFAULT 0;
