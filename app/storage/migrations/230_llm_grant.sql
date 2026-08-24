-- «Одолжить свою модель конкретному человеку» (v2.31.x).
--
-- Участник подключает СВОЙ ключ на /settings/llm и платит за себя. Этот срез
-- добавляет ЯВНОЕ, ПОИМЁННОЕ исключение: человек может дать доступ к своей
-- модели конкретному другу с ОБЯЗАТЕЛЬНЫМ дневным лимитом. Никаких «всем
-- друзьям» и никакого «по умолчанию включено» — только штучная выдача.
--
-- РЕШЕНИЯ ПО СХЕМЕ (читать перед правками):
--
-- 1. UNIQUE(grantor_id, grantee_id) — на пару «кто дал → кому дал» живёт РОВНО
--    одна строка. Повторная выдача не плодит вторую, а обновляет существующую
--    (UPSERT в app/llm/grants.py ``upsert_grant``): иначе после одного отзыва
--    пара навсегда обрастала бы мусорными строками и «какая из них главная?»
--    становилось бы неопределённым.
--
-- 2. ``daily_limit`` — NOT NULL + CHECK > 0. Лимит здесь не «опция», а условие
--    существования выдачи: это чужие деньги/чужое железо, и бесконечный доступ
--    к чужому кошельку не должен быть выразим в схеме вообще.
--
-- 3. Отзыв — ДВА независимых сигнала: ``enabled`` (временная пауза, строку
--    можно включить обратно с той же историей) и ``revoked_at`` (насовсем).
--    Резолвер требует ОБА: enabled = 1 И revoked_at IS NULL.
--
-- 4. Расход считаем ОТДЕЛЬНОЙ таблицей-счётчиком ``llm_grant_usage``, а не
--    подсчётом строк ``llm_usage``. Причины: (а) точность — llm_usage пишется
--    best-effort и её сбой намеренно проглатывается (см. ``_record_usage``),
--    то есть квота, посчитанная по ней, тихо протекала бы; (б) цена — проверка
--    лимита это чтение ОДНОЙ строки по PK, а не агрегат по журналу за день;
--    (в) атомарность — инкремент и проверка лимита выражаются одним
--    INSERT ... ON CONFLICT DO UPDATE ... WHERE, то есть гонка двух параллельных
--    запросов не может выдать N+1-й вызов.
--
-- 5. ``day`` — строка 'YYYY-MM-DD' в ЛОКАЛЬНОЙ дате сервера (см. ``_today`` в
--    app/llm/grants.py). Сброс квоты = просто другой ключ в PK, никакого
--    крон-обнуления нет и не нужно.
--
-- FK включены глобально (``PRAGMA foreign_keys = ON`` в app/storage/db.py
-- ``_configure_connection``), поэтому ON DELETE CASCADE реально срабатывает:
-- удаление аккаунта уносит и выданные им, и полученные им доступы, а вместе
-- с выдачей уходит и её счётчик расхода.

CREATE TABLE IF NOT EXISTS llm_grant (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    grantor_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    grantee_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    daily_limit INTEGER NOT NULL DEFAULT 50,
    enabled     INTEGER NOT NULL DEFAULT 1,
    note        TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    revoked_at  TEXT,
    UNIQUE (grantor_id, grantee_id),
    CHECK (grantor_id <> grantee_id),
    CHECK (daily_limit > 0),
    CHECK (enabled IN (0, 1))
);

-- Горячий запрос резолвера: «есть ли у ЭТОГО пользователя живая выдача».
-- Ведущая колонка grantee_id + оба флага активности прямо в индексе.
CREATE INDEX IF NOT EXISTS idx_llm_grant_grantee
    ON llm_grant(grantee_id, enabled, revoked_at);

-- Страница «Я делюсь»: все выдачи одного человека.
CREATE INDEX IF NOT EXISTS idx_llm_grant_grantor
    ON llm_grant(grantor_id, revoked_at);

CREATE TABLE IF NOT EXISTS llm_grant_usage (
    grant_id INTEGER NOT NULL REFERENCES llm_grant(id) ON DELETE CASCADE,
    day      TEXT    NOT NULL,
    used     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (grant_id, day)
);
