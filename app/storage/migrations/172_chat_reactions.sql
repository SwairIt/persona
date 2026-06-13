-- T30 — реакции на ответы ИИ в чате (🤔 не понял / ✅ помогло / 🔥 / ❤️ / 😕 / ⚠️).
-- Одна реакция на сообщение от пользователя (UNIQUE message_id+user_id), toggle.
-- ИИ учитывает последнюю реакцию в следующем ответе (см. chat_sessions.py).

CREATE TABLE IF NOT EXISTS chat_reaction (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id  INTEGER NOT NULL,
    user_id     INTEGER,
    reaction    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_reaction_uniq ON chat_reaction(message_id, user_id);
CREATE INDEX IF NOT EXISTS idx_chat_reaction_msg ON chat_reaction(message_id);
