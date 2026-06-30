-- v201 — durable system_log: кросс-воркерное хранилище логов уровня
-- warning+ для пульта владельца (/root).
--
-- Зачем
-- -----
-- In-memory ``deque`` в :mod:`app.log_buffer` живёт в каждом воркере
-- отдельно, поэтому ``/root/logs/recent.json`` раньше показывал логи
-- только того воркера, что обслужил запрос. Эта таблица собирает
-- значимые события (level >= warning) от ВСЕХ воркеров в одно место,
-- чтобы пульт мог отдать сводную картину.
--
-- Заметки по дизайну
-- ------------------
-- * ``ts`` — ISO-8601 из ``datetime('now')`` (UTC), как у audit_log.
-- * ``worker_id`` — PID воркера (строка), чтобы различать источники.
-- * ``extra`` — свободный TEXT (JSON-строка нескольких безопасных полей);
--   секреты в неё НЕ попадают (фильтрация на стороне log_buffer).
-- * Пишем best-effort и только warning+, чтобы не бить по I/O:
--   мгновенный live-поток по-прежнему идёт через in-memory deque + SSE.
-- * ``IF NOT EXISTS`` на таблице и индексах — идемпотентность при
--   повторных прогонах ``init_database``.

CREATE TABLE IF NOT EXISTS system_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    worker_id TEXT,
    level TEXT,
    logger TEXT,
    event TEXT,
    extra TEXT
);

CREATE INDEX IF NOT EXISTS idx_system_log_ts ON system_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_system_log_level ON system_log(level);
