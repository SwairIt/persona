-- 186_vec_memory.sql — векторная память (sqlite-vec), ОПЦИОНАЛЬНО.
-- Если расширение sqlite-vec НЕ установлено, CREATE VIRTUAL TABLE USING vec0
-- падает с 'no such module' и пропускается раннером (см. _IDEMPOTENT_ALTER_ERRORS).
-- Тогда весь векторный путь тихо отключается, а FTS5/LIKE recall работают как раньше.
--
-- Размерность 768 = nomic-embed-text (дефолтная embed-модель). Если сменишь
-- модель с другой размерностью (bge-m3=1024) — нужна новая vec0-таблица.
-- Side-таблица vec_message_meta хранит соответствие rowid→message/user (на случай,
-- если понадобится фильтрация; KNN по vec0 + JOIN).
CREATE VIRTUAL TABLE IF NOT EXISTS chat_message_vec USING vec0(
    message_id INTEGER PRIMARY KEY,
    embedding FLOAT[768]
);

-- Метаданные для фильтра по пользователю/сессии (vec0 не хранит произвольные колонки
-- для фильтра в старых версиях — держим отдельно, обычная таблица всегда создаётся).
CREATE TABLE IF NOT EXISTS vec_message_meta (
    message_id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL,
    session_id INTEGER,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_vec_meta_user ON vec_message_meta(user_id);
