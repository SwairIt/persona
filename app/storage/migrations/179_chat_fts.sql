-- 179_chat_fts.sql — Phase 6 (DB search): FTS5 для поиска по сообщениям чата.
-- LIKE '%q%' = полный скан (медленно на больших объёмах). FTS5 + bm25 = sub-ms
-- даже на 100k+ сообщений. External-content таблица + триггеры синхронизации.

CREATE VIRTUAL TABLE IF NOT EXISTS chat_message_fts USING fts5(
    content,
    content='chat_message',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS chat_message_ai AFTER INSERT ON chat_message
BEGIN
    INSERT INTO chat_message_fts(rowid, content) VALUES (new.id, COALESCE(new.content, ''));
END;

CREATE TRIGGER IF NOT EXISTS chat_message_ad AFTER DELETE ON chat_message
BEGIN
    INSERT INTO chat_message_fts(chat_message_fts, rowid, content)
    VALUES ('delete', old.id, COALESCE(old.content, ''));
END;

CREATE TRIGGER IF NOT EXISTS chat_message_au AFTER UPDATE ON chat_message
BEGIN
    INSERT INTO chat_message_fts(chat_message_fts, rowid, content)
    VALUES ('delete', old.id, COALESCE(old.content, ''));
    INSERT INTO chat_message_fts(rowid, content) VALUES (new.id, COALESCE(new.content, ''));
END;

-- Backfill existing rows from the content table (one-time, idempotent rebuild).
INSERT INTO chat_message_fts(chat_message_fts) VALUES ('rebuild');
