-- 196_dream_report.sql — журнал ночных циклов «сна» (docs/MEMORY_RESEARCH.md §3).
-- Одна строка на завершённый run_dream_cycle: сколько кандидатов извлечено,
-- сколько промоутнуто в user_memory, сколько фактов слито (Phase 3b), сколько
-- конфликтов разрешено, текст REM-нарратива и интегральный impact_score
-- (promoted / max(1, candidates)). Питает будущий UI-отчёт «что я выучил за
-- ночь» (слайс S6) и метрику пользы «обучения». Недеструктивно, только запись.
-- Идемпотентно (CREATE TABLE/INDEX IF NOT EXISTS — раннер глотает повторный
-- прогон). Без расширений.
CREATE TABLE IF NOT EXISTS dream_report (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    fired_at TEXT NOT NULL DEFAULT (datetime('now')),
    candidates INTEGER NOT NULL DEFAULT 0,     -- извлечено кандидатов (Light Sleep)
    promoted INTEGER NOT NULL DEFAULT 0,       -- промоутнуто в user_memory (Deep Sleep)
    consolidations INTEGER NOT NULL DEFAULT 0, -- слияний дублей (Phase 3b)
    conflicts INTEGER NOT NULL DEFAULT 0,      -- разрешено противоречий (update/delete)
    dream_text TEXT,                           -- REM-нарратив тем недели (опц.)
    impact_score REAL,                         -- promoted / max(1, candidates)
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
-- Лента отчётов пользователя — свежие сверху.
CREATE INDEX IF NOT EXISTS idx_dream_report_user
    ON dream_report(user_id, id DESC);
