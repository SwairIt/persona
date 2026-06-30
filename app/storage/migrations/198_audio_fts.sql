-- 198_audio_fts.sql — S12: FTS5-индекс по audio_segment.transcript.
-- Раньше поиск (app/web/routes/audio_search.py) шёл полным сканом LIKE '%q%'.
-- При накоплении транскриптов это медленно; FTS5 + bm25 = sub-ms даже на
-- десятках тысяч сегментов. External-content таблица (content='audio_segment',
-- content_rowid='id') + триггеры синка ins/upd/del, как у chat_message_fts (179).
-- Всё идемпотентно (IF NOT EXISTS + rebuild) — безопасно при повторном прогоне.

CREATE VIRTUAL TABLE IF NOT EXISTS audio_segment_fts USING fts5(
    transcript,
    content='audio_segment',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

-- Синк: после INSERT в audio_segment кладём строку в FTS (NULL → '').
CREATE TRIGGER IF NOT EXISTS audio_segment_ai AFTER INSERT ON audio_segment
BEGIN
    INSERT INTO audio_segment_fts(rowid, transcript)
    VALUES (new.id, COALESCE(new.transcript, ''));
END;

-- Синк: после DELETE — спецстрока 'delete' для external-content FTS5.
CREATE TRIGGER IF NOT EXISTS audio_segment_ad AFTER DELETE ON audio_segment
BEGIN
    INSERT INTO audio_segment_fts(audio_segment_fts, rowid, transcript)
    VALUES ('delete', old.id, COALESCE(old.transcript, ''));
END;

-- Синк: после UPDATE — удаляем старую версию, вставляем новую.
CREATE TRIGGER IF NOT EXISTS audio_segment_au AFTER UPDATE ON audio_segment
BEGIN
    INSERT INTO audio_segment_fts(audio_segment_fts, rowid, transcript)
    VALUES ('delete', old.id, COALESCE(old.transcript, ''));
    INSERT INTO audio_segment_fts(rowid, transcript)
    VALUES (new.id, COALESCE(new.transcript, ''));
END;

-- Начальный бэкфилл из content-таблицы (одноразовый, идемпотентный rebuild).
INSERT INTO audio_segment_fts(audio_segment_fts) VALUES ('rebuild');
