-- 203_llm_job.sql — очередь задач «Persona LLM Worker» (срез W-A).
-- Цель: убрать devtunnel. Сервер кладёт задачи (chat/embed) в очередь в БД,
-- ПК-воркер делает ИСХОДЯЩИЕ long-poll-запросы, забирает задачу, считает на
-- локальной Ollama и шлёт ответ обратно по HTTP. Всё чистый HTTP — дружит с
-- FastPanel-прокси, без WebSocket. Аддитивно: новый провайдер 'worker'; пока
-- не переключат — текущий ollama-путь не трогается.
--
-- Идемпотентно: CREATE TABLE/INDEX IF NOT EXISTS; повторный прогон — no-op.
--
-- kind: 'chat' | 'embed'.
-- payload (JSON): chat = {messages:[{role,content}], options:{}};
--                 embed = {prompt, options:{}}.
-- result (для embed) = JSON-вектор (список float).
CREATE TABLE IF NOT EXISTS llm_job (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER,
    kind        TEXT    NOT NULL DEFAULT 'chat',   -- chat | embed
    model       TEXT,
    payload     TEXT,                              -- JSON-вход задачи
    status      TEXT    NOT NULL DEFAULT 'pending',-- pending | streaming | done | error
    worker_id   TEXT,                              -- кто забрал задачу
    result      TEXT,                              -- для embed: JSON-вектор
    error       TEXT,                              -- текст ошибки воркера
    created_at  TEXT    DEFAULT (datetime('now')),
    claimed_at  TEXT,                              -- момент claim_next
    finished_at TEXT                               -- момент finish_job
);

-- Стрим-чанки ответа (для chat): воркер шлёт токены по seq, сервер отдаёт их
-- наружу как дельты OllamaClient.stream.
CREATE TABLE IF NOT EXISTS llm_job_chunk (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id     INTEGER,
    seq        INTEGER,
    content    TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Горячий путь claim_next — выбор самой старой pending-задачи по (status, id).
CREATE INDEX IF NOT EXISTS idx_llm_job_status ON llm_job(status, id);
-- Горячий путь read_chunks — чанки одной задачи по возрастанию seq.
CREATE INDEX IF NOT EXISTS idx_llm_job_chunk ON llm_job_chunk(job_id, seq);
