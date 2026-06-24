-- 191_reflections.sql — procedural-ярус памяти (docs/MEMORY_RESEARCH.md §2.3):
-- инсайты ночной рефлексии (Generative-Agents reflection tree, kind='insight')
-- + REM-дневник «снов» (Hermes DREAMS.md эквивалент, kind='dream') + Reflexion-
-- заметки (kind='self_note'). Bi-temporal soft-invalidate как у user_memory:
-- valid_until IS NULL → актуальная запись, штамп времени → ушла из выдачи, но
-- осталась в истории. Идемпотентно (CREATE TABLE/INDEX IF NOT EXISTS — раннер
-- глотает повторный прогон). Без расширений.
CREATE TABLE IF NOT EXISTS reflection (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    kind TEXT NOT NULL DEFAULT 'insight',   -- insight|dream|self_note
    text TEXT NOT NULL,
    source_message_ids TEXT,                 -- JSON-массив id (цитаты «because of …»)
    importance REAL,                         -- 1..10
    valid_until TEXT,                        -- soft-invalidate, как user_memory
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_reflection_active
    ON reflection(user_id, kind, id DESC) WHERE valid_until IS NULL;
