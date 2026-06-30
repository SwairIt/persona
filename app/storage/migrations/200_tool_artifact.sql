-- 200_tool_artifact.sql — артефакты вызовов инструментов (F6-05).
-- Скриншоты браузер-агента (и др. файловые артефакты) линкуются к строке
-- журнала активности (tool_execution) через exec_id. Окно активности
-- (/ai-activity) рендерит превью/ссылку на /workspace/file/{path_in_workspace}.
--
-- path_in_workspace — ОТНОСИТЕЛЬНЫЙ путь ВНУТРИ воркспейса пользователя
-- (например 'browse/agent-12-1750000000.png'); абсолютные пути сюда не пишем —
-- отдаём наружу только под префиксом /workspace/file/ (безопасность).
--
-- Идемпотентно: CREATE TABLE/INDEX IF NOT EXISTS; повторный прогон — no-op.
CREATE TABLE IF NOT EXISTS tool_artifact (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exec_id INTEGER,
    type TEXT,                                 -- screenshot|file|...
    mime_type TEXT,                            -- image/png, ...
    path_in_workspace TEXT,                    -- относительный путь внутри /workspace
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (exec_id) REFERENCES tool_execution(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tool_artifact_exec ON tool_artifact(exec_id);
