-- v0.98 feature 2/3 — LLM usage stats (per-call token bookkeeping).
--
-- Background
-- ----------
-- Four independent code paths burn the user's BYO API budget: the daily
-- digest pipeline (:mod:`app.llm.summariser`), the OCR-via-vision path
-- (:mod:`app.llm.ocr_via_vision`), the OCR translation feature
-- (:mod:`app.llm.ocr_translate`) and the per-day TL;DR
-- (:mod:`app.llm.day_tldr`). Until v0.98 there was no record of how many
-- tokens each path consumed — an operator burning through a paid quota
-- could only inspect their provider dashboard, not Persona itself.
--
-- This migration introduces a tiny append-only ledger row written by the
-- :func:`app.llm.client.make_client` wrapper after every successful (or
-- failed) completion. ``/stats/llm-usage`` reads it back and renders a
-- 30-day line chart + per-kind breakdown so the operator can spot which
-- feature is the biggest spender.
--
-- Columns
-- -------
--   * ``ts``            — ISO-8601 wall-clock from ``datetime('now')``,
--                         matching :file:`037_audit_log.sql` and
--                         :file:`076_ocr_history.sql`. Indexed because the
--                         /stats/llm-usage page filters by recent N days.
--   * ``kind``          — short slug identifying the calling feature:
--                         ``"digest"`` / ``"vision"`` / ``"translate"`` /
--                         ``"day_tldr"`` / ``"auto_tag"`` / ``"qa"`` /
--                         ``"note_draft"`` / ``"weekly"`` / ``"monthly"``
--                         / ``"per_app"`` / ``"unknown"``. Stored for the
--                         per-kind aggregation column on the UI; never
--                         parsed back into code paths.
--   * ``provider``      — ``"anthropic"`` / ``"openai"`` / ``"groq"`` so
--                         the breakdown can distinguish a Sonnet-heavy
--                         month from a gpt-4o-mini-heavy month even if
--                         the operator swaps providers mid-billing.
--   * ``input_tokens``  — prompt tokens reported by the provider's
--                         ``usage.input_tokens`` (Anthropic) or
--                         ``usage.prompt_tokens`` (OpenAI/Groq). NULL
--                         when the response was malformed or the
--                         provider didn't include usage.
--   * ``output_tokens`` — completion tokens (same caveat as above).
--   * ``success``       — 1 when the underlying client returned text,
--                         0 when it raised. The wrapper still records
--                         on failure so the operator can see a
--                         transient outage in the chart.
--
-- ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` keep
-- the migration idempotent across re-runs of ``init_database``.

CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    kind TEXT NOT NULL,
    provider TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    success INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_ts
    ON llm_usage(ts);
