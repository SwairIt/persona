-- 180_user_memory.sql — Phase 1: личная память ассистента (факты о пользователе).
-- Отдельно от неявного keyword-recall: это КУРИРУЕМЫЕ факты («кто ты»), которые
-- ИИ помнит между чатами и подмешивает в системный промпт. /remember, /forget,
-- инструмент query_memory читают её. Авто-извлечение фактов — следующая итерация.
CREATE TABLE IF NOT EXISTS user_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'fact',     -- fact|preference|person|project|reminder|other
    text TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    source_session_id INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_user_memory_user ON user_memory(user_id, pinned DESC, id DESC);
