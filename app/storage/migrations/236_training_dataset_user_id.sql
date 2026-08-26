-- Утечка: датасет дообучения собирал ПЕРЕПИСКУ УЧАСТНИКОВ (2026-08-26).
--
-- ЧТО БЫЛО. ``training_dataset`` (миграция 162) пишется после КАЖДОГО ответа
-- ассистента и держит полный текст: ``user_text``, ``assistant_text``,
-- ``system_prompt`` и предыдущие реплики (``context_json``). Колонки
-- ``user_id`` у неё не было вовсе, сбор включён по умолчанию
-- (kv ``training_dataset_enabled='1'``), а владельческий экспорт
-- ``/admin/dataset/export.jsonl`` выбирал таблицу целиком. С открытой
-- публичной регистрацией (v2.33.1) это значит буквально следующее: личный
-- разговор постороннего человека собирался, уезжал владельцу одним файлом и
-- дальше — в веса дообученной модели. Политика конфиденциальности такого
-- сбора не описывала, потому что его не должно быть.
--
-- ЧТО ДЕЛАЕТ ЭТА МИГРАЦИЯ.
--   1. Добавляет ``user_id`` — чтобы у строки был автор, а у выборки было по
--      чему фильтровать (рубеж 2 в app/training/collector.py).
--   2. Проставляет его задним числом из ``chat_session.user_id`` по
--      ``session_id``.
--   3. УДАЛЯЕТ всё, что не принадлежит владельцу.
--
-- ПОЧЕМУ УДАЛЯЕТ, А НЕ ПОМЕЧАЕТ. Эти строки — чужой личный текст, который не
-- имел права быть записанным. «Оставим на всякий случай, экспорт же теперь
-- фильтруется» — ровно тот инстинкт, из-за которого утечки живут годами:
-- фильтр можно случайно снять одной строкой, а снимок базы
-- (``/settings/privacy/snapshot``) и резервные копии отдают файл целиком, мимо
-- любого SQL-фильтра. Единственное состояние, в котором чужого текста тут нет,
-- — то, где его физически нет.
--
-- НЕАТРИБУТИРУЕМЫЕ СТРОКИ (``user_id IS NULL`` после backfill) удаляются тоже.
-- Это строки, чей ``session_id`` уже никуда не ведёт: NULL по
-- ``ON DELETE SET NULL`` после удаления чата, либо запись из скрипта. Автора у
-- них не установить НИКАК, значит нельзя доказать, что текст владельческий, —
-- а «наверное, владельца» не является основанием хранить чужую переписку.
--
-- КТО СЧИТАЕТСЯ ВЛАДЕЛЬЦЕМ. Ровно то же правило, что у ``app.auth.owner``:
-- kv ``owner_user_id``, если он задан и числовой, иначе ``MIN(users.id)``;
-- плюс делегаты с полным доступом из kv ``full_access_user_ids`` (список id
-- через запятую). Правило продублировано тут на SQL намеренно: миграция и
-- рантайм-фильтр обязаны совпадать, иначе часть строк окажется «записана, но
-- невыгружаема» или наоборот.
--
-- ВНИМАНИЕ ПЕРЕД ПРИМЕНЕНИЕМ НА ЖИВОЙ БАЗЕ: удаляется всё, что не принадлежит
-- ТЕКУЩЕМУ значению kv ``owner_user_id``. Если этот ключ смотрит не на тот
-- аккаунт (например, его перезаписал тестовый прогон), уедут и строки
-- настоящего владельца. Проверьте ``SELECT value FROM kv_settings WHERE
-- key='owner_user_id'`` ДО деплоя.

ALTER TABLE training_dataset ADD COLUMN user_id INTEGER;

UPDATE training_dataset
   SET user_id = (
       SELECT s.user_id FROM chat_session s WHERE s.id = training_dataset.session_id
   )
 WHERE user_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_training_dataset_user
    ON training_dataset(user_id);

WITH RECURSIVE
  -- Первичный владелец: kv ``owner_user_id`` (если он целое число), иначе
  -- младший id в ``users`` — как в app/auth/owner.py::get_owner_user_id.
  owner_primary(id) AS (
    SELECT CASE
      WHEN (SELECT TRIM(value) FROM kv_settings WHERE key = 'owner_user_id') <> ''
       AND (SELECT TRIM(value) FROM kv_settings WHERE key = 'owner_user_id')
           NOT GLOB '*[^0-9]*'
      THEN CAST((SELECT TRIM(value) FROM kv_settings WHERE key = 'owner_user_id') AS INTEGER)
      ELSE (SELECT MIN(id) FROM users)
    END
  ),
  -- kv ``full_access_user_ids`` — "3,7;9" → построчно. Разбор рекурсивным CTE,
  -- потому что в SQLite нет split(); ';' сводим к ',' как это делает
  -- app/auth/owner.py::_full_access_ids.
  full_access(rest, one) AS (
    SELECT REPLACE(
             COALESCE((SELECT value FROM kv_settings WHERE key = 'full_access_user_ids'), ''),
             ';', ','
           ) || ',', ''
    UNION ALL
    SELECT SUBSTR(rest, INSTR(rest, ',') + 1),
           TRIM(SUBSTR(rest, 1, INSTR(rest, ',') - 1))
      FROM full_access
     WHERE rest <> ''
  ),
  owner_ids(id) AS (
    SELECT id FROM owner_primary WHERE id IS NOT NULL
    UNION
    SELECT CAST(one AS INTEGER) FROM full_access
     WHERE one <> '' AND one NOT GLOB '*[^0-9]*'
  )
DELETE FROM training_dataset
 WHERE user_id IS NULL
    OR user_id NOT IN (SELECT id FROM owner_ids);
