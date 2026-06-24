-- 192_memory_salience.sql — salience/recency/частота для scoring recall
-- (docs/MEMORY_RESEARCH.md §2.3, Фаза B). Колонки на chat_message (episodic-
-- скоринг: importance/last_seen/access_count) и user_memory (semantic-скоринг +
-- PII-флаг). Питают Generative-Agents-пересортировку score_and_rerank
-- (recency·importance·relevance) и rehearsal/decay-reset (бамп last_seen+
-- access_count на каждом recall). Идемпотентно: раннер глотает duplicate column
-- на повторном ALTER ADD COLUMN, индекс — IF NOT EXISTS. Без расширений.
--
-- Bi-temporal (valid_until, superseded_by) у user_memory УЖЕ есть (миграция 187)
-- — это и есть Graphiti-style invalidate-not-delete, достраивать не нужно.
ALTER TABLE chat_message ADD COLUMN importance   INTEGER;                 -- 1..10, NULL=не оценено
ALTER TABLE chat_message ADD COLUMN last_seen    TEXT;                    -- ISO, бамп при recall (rehearsal)
ALTER TABLE chat_message ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE user_memory  ADD COLUMN salience          REAL;              -- 1..10 (llm|эвристика)
ALTER TABLE user_memory  ADD COLUMN importance_source TEXT;              -- 'llm'|'heuristic'
ALTER TABLE user_memory  ADD COLUMN last_seen         TEXT;
ALTER TABLE user_memory  ADD COLUMN access_count      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE user_memory  ADD COLUMN redacted          INTEGER NOT NULL DEFAULT 0;  -- PII-флаг

-- Горячий путь semantic-скоринга — только актуальные факты, по убыванию salience.
CREATE INDEX IF NOT EXISTS idx_user_memory_salience
    ON user_memory(user_id, salience DESC) WHERE valid_until IS NULL;
