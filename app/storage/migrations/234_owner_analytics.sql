-- 234_owner_analytics.sql — ПЕРВОСТОРОННЯЯ аналитика владельца инстанса.
--
-- ЗАЧЕМ. До этой миграции единственная аналитика сайта — Яндекс.Метрика
-- (счётчик 111901324), и она:
--   * работает ТОЛЬКО на публичных страницах и ТОЛЬКО после согласия;
--   * ничего не знает про роли (владелец/участник/аноним) — а владелец сам
--     ходит по своему сайту чаще всех и портит себе же статистику;
--   * не умеет отвечать на вопрос «сколько человек дошло от лендинга до
--     первого сообщения в чате», потому что не видит наших таблиц.
-- Эти три таблицы дают владельцу его собственный ответ, не выходя за пределы
-- его же сервера.
--
-- ЧЕГО ЗДЕСЬ НАМЕРЕННО НЕТ (и это не забывчивость)
-- ------------------------------------------------
--   * НЕТ колонки под сырой user-agent. Хранится только грубый класс
--     ``device`` (desktop/mobile/bot/unknown) — по UA-строке человека можно
--     доузнать (fingerprint), по слову «mobile» — нет.
--   * НЕТ колонки под IP, ни полный, ни усечённый, ни хешированный. IP — это
--     персональные данные по позиции Роскомнадзора, а на вопросы владельца
--     («сколько зарегалось, что нажимают, куда заходят») он не отвечает
--     вообще. Не храним — значит нечего утекать и нечего удалять по запросу.
--   * НЕТ полного реферера. Только ХОСТ (``referrer_host``): полный URL чужой
--     страницы часто содержит поисковый запрос и идентификаторы сессии на
--     чужом сайте — то есть данные третьих лиц, которые нас не касаются.
--   * НЕТ ни одного поля со СМЫСЛОМ действия: ни текста запроса, ни
--     содержимого формы, ни значений полей. ``label`` — это подпись элемента,
--     которую разработчик сам проставил в ``data-track``, а не то, что человек
--     ввёл. Это счётчик, а не запись сессии: вебвизора здесь нет и не будет.
--
-- analytics_event — СЫРЫЕ события, окно ОГРАНИЧЕНО
-- ------------------------------------------------
-- Живут ``analytics_retention_days`` суток (kv, по умолчанию 90), дальше их
-- сносит :func:`app.analytics.store.purge_old_events`. Сутки считаются по UTC
-- — ровно как ``users.created_at`` и весь остальной ``datetime('now')`` в этой
-- базе; смешивать часовые пояса в одном отчёте хуже, чем объявить один.
--
-- ``session_hash`` — ПСЕВДОНИМ, а не идентификатор человека:
--   * у вошедшего это HMAC от токена сессии — он умирает вместе с сессией и
--     обратно в токен не разворачивается;
--   * у анонима с согласием — HMAC от (соль инстанса + сутки + класс
--     устройства), то есть склейка держится один день и не переживает
--     полночь;
--   * у анонима БЕЗ согласия он NULL — такой визит считается как обезличенный
--     хит, и связать два его хита между собой невозможно by construction.
-- Соль (kv ``analytics_salt``) генерируется на инстансе и никуда не уезжает.
--
-- ``user_id`` с ON DELETE CASCADE: поведенческий след вошедшего участника —
-- его персональные данные, и право на удаление обязано уносить их вместе с
-- аккаунтом, без отдельного кода в app/auth/account_delete.py.
CREATE TABLE IF NOT EXISTS analytics_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at   TEXT    NOT NULL,
    day           TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    path          TEXT    NOT NULL,
    label         TEXT    NOT NULL DEFAULT '',
    role          TEXT    NOT NULL,
    device        TEXT    NOT NULL DEFAULT 'unknown',
    referrer_host TEXT    NOT NULL DEFAULT '',
    session_hash  TEXT,
    user_id       INTEGER REFERENCES users(id) ON DELETE CASCADE,
    first_view    INTEGER NOT NULL DEFAULT 0,
    status        INTEGER
);

-- Свёртка суток и вычистка окна ходят по (day) — самый горячий доступ.
CREATE INDEX IF NOT EXISTS idx_analytics_event_day
    ON analytics_event(day, kind);

-- «Живые» цифры за последние 15 минут читаются по времени.
CREATE INDEX IF NOT EXISTS idx_analytics_event_at
    ON analytics_event(occurred_at);

-- Уникальные сессии суток и DAU считаются группировкой по псевдониму.
CREATE INDEX IF NOT EXISTS idx_analytics_event_session
    ON analytics_event(day, session_hash);

-- analytics_daily — СВЁРНУТЫЕ сутки, живут вечно
-- ----------------------------------------------
-- Дашборд НИКОГДА не сканирует analytics_event за 30 дней: закрытые сутки
-- сворачиваются один раз в эту таблицу, и отчёт читает уже её. Идентификаторов
-- тут нет вовсе (ни session_hash, ни user_id) — поэтому строки безопасно
-- хранить дольше окна сырых событий, а покидать сервер им всё равно некуда.
CREATE TABLE IF NOT EXISTS analytics_daily (
    day           TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    path          TEXT    NOT NULL,
    label         TEXT    NOT NULL DEFAULT '',
    role          TEXT    NOT NULL,
    device        TEXT    NOT NULL DEFAULT 'unknown',
    referrer_host TEXT    NOT NULL DEFAULT '',
    hits          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, kind, path, label, role, device, referrer_host)
);

CREATE INDEX IF NOT EXISTS idx_analytics_daily_day
    ON analytics_daily(day, kind);

-- Уникальные сессии/люди за сутки — отдельной строкой, потому что COUNT
-- DISTINCT по свёрнутым счётчикам восстановить нельзя (сумма хитов ≠ число
-- сессий). Считается ОДИН раз при свёртке, пока сырые события ещё живы.
CREATE TABLE IF NOT EXISTS analytics_daily_unique (
    day      TEXT    NOT NULL,
    role     TEXT    NOT NULL,
    sessions INTEGER NOT NULL DEFAULT 0,
    users    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, role)
);

-- analytics_user_day — один бит «этот аккаунт был активен в этот день»
-- --------------------------------------------------------------------
-- Нужен ровно для двух чисел: DAU/WAU и удержание когорт («из тех, кто
-- зарегался на прошлой неделе, вернулся ли кто-нибудь»). Ни путей, ни
-- действий, ни времени внутри суток тут нет — по строке нельзя узнать, ЧТО
-- человек делал, только что он заходил. Меньший объём данных на этот вопрос
-- не отвечает, больший — не нужен.
CREATE TABLE IF NOT EXISTS analytics_user_day (
    day     TEXT    NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (day, user_id)
);

CREATE INDEX IF NOT EXISTS idx_analytics_user_day_user
    ON analytics_user_day(user_id, day);
