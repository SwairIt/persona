-- 194_perf_indexes.sql — индексы под горячие пути памяти/recall.
--
-- Два covering-индекса, ускоряющие:
--   idx_user_memory_valid    — выборку списка активной памяти пользователя
--                              (фильтр по user_id + valid_until, сортировка id DESC);
--   idx_chat_session_user_id — JOIN chat_session при recall по чатам пользователя.
-- Оба безопасны и идемпотентны: CREATE INDEX IF NOT EXISTS, повторный прогон
-- ничего не ломает. Без расширений.

CREATE INDEX IF NOT EXISTS idx_user_memory_valid
    ON user_memory(user_id, valid_until, id DESC);

CREATE INDEX IF NOT EXISTS idx_chat_session_user_id
    ON chat_session(user_id, id);
