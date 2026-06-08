-- T28 (2026-06-08) — code write target.
--
-- Юзер хочет per-account выбрать одно устройство куда будут попадать
-- файлы написанные AI через write_file tool. Например в одном аккаунте
-- Mac + iPhone — выбирает Mac. У другого юзера Windows + Android —
-- выбирает Windows. Файлы пишутся в общий workspace на сервере (там
-- они каноничны), но помечаются для синхронизации на target device.
--
-- Реализация:
--   * device.is_code_write_target — boolean, ровно одна строка на
--     user_id может быть = 1 (поддерживается уровнем приложения).
--   * Когда AI вызывает write_file → файл создаётся в workspace + в
--     таблицу workspace_file_event пишется запись для последующей
--     синхронизации.
--   * Device agent на target машине дёргает /api/workspace/sync,
--     получает новые файлы, кладёт в ~/persona-workspace/ локально.

ALTER TABLE device ADD COLUMN is_code_write_target INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS workspace_file_event (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    relative_path   TEXT NOT NULL,
    operation       TEXT NOT NULL CHECK (operation IN ('write', 'delete')),
    content_bytes   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_workspace_file_event_user
    ON workspace_file_event(user_id, id);
