-- 181_tool_activity.sql — окно активности: журнал выполнения инструментов ИИ.
-- Чтобы пользователь ВИДЕЛ, что делает ИИ (какие инструменты, с чем, результат).
-- Заполняется из чат-цикла (send-stream), стримится по SSE (type=activity),
-- и доступен для replay по сессии. Artifact'ы (скриншоты браузер-агента) добавим
-- отдельной таблицей, когда появится браузер-агент.
CREATE TABLE IF NOT EXISTS tool_execution (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    session_id INTEGER,
    message_id INTEGER,
    seq INTEGER DEFAULT 0,
    kind TEXT NOT NULL DEFAULT 'builtin',     -- builtin|browser|mcp
    tool_name TEXT NOT NULL,
    args_json TEXT,
    status TEXT NOT NULL DEFAULT 'running',    -- running|done|error
    result_text TEXT,
    error_text TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT,
    elapsed_ms INTEGER,
    FOREIGN KEY (session_id) REFERENCES chat_session(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tool_exec_session ON tool_execution(session_id, id);
CREATE INDEX IF NOT EXISTS idx_tool_exec_user ON tool_execution(user_id, started_at DESC);
