-- T21 (2026-06-07) — Unlimited chat memory + per-message timing.
--
-- ``chat_session.summary``: when a session grows past ~50 messages the
-- background "summarizer" rolls the oldest messages into a compact text
-- block stored here. ``build_history_for_llm`` then prepends this
-- summary to the last ~50 actual messages, so the model effectively
-- sees the entire conversation regardless of length.
--
-- ``chat_session.summary_up_to_id``: the id of the last message
-- INCLUDED in the summary. Anything with id > this value is fresh and
-- gets fed verbatim to the model. Lets the summariser do incremental
-- work without re-reading old messages.
--
-- ``chat_message.elapsed_ms``: per-assistant-turn server-side latency
-- from "form POST received" to "LLM response complete". Shown in the
-- UI as "ответ за 12.3s" so the user sees how their local Qwen is
-- behaving.
--
-- ``chat_message.input_tokens`` / ``output_tokens``: parsed from the
-- LLM provider's response when available. Powers the live context
-- usage indicator in the UI.

ALTER TABLE chat_session ADD COLUMN summary TEXT;
ALTER TABLE chat_session ADD COLUMN summary_up_to_id INTEGER NOT NULL DEFAULT 0;

ALTER TABLE chat_message ADD COLUMN elapsed_ms INTEGER;
ALTER TABLE chat_message ADD COLUMN input_tokens INTEGER;
ALTER TABLE chat_message ADD COLUMN output_tokens INTEGER;
