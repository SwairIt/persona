-- T31 ФАЗА F — голосовой ассистент на Mac.
-- Mac-агент слушает микрофон, по wake-word «Персона» шлёт распознанную
-- фразу на /api/voice/utterance; сервер генерирует ответ в выбранном чате
-- и кладёт его в очередь voice_tts; агент опрашивает /api/voice/pending и
-- проигрывает ответ через macOS `say`. Захват микрофона и say — на Mac
-- (нужна проверка на Mac); сервер хранит настройки и очередь.
CREATE TABLE IF NOT EXISTS voice_tts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL,
    session_id   INTEGER,
    text         TEXT    NOT NULL,
    voice        TEXT,                                  -- имя голоса say (опц.)
    status       TEXT    NOT NULL DEFAULT 'pending',    -- pending | done
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_voice_tts_pending ON voice_tts(status, id);

-- Настройки голоса (правятся на /settings/voice владельцем).
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('voice_enabled', '0');
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('voice_session_id', '');
INSERT OR IGNORE INTO kv_settings (key, value) VALUES ('voice_name', '');
