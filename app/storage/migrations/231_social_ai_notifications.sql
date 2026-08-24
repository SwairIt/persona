-- ИИ-ответы в личных сообщениях + уведомления социального слоя (v2.31.x).
--
-- Срез 2 поверх 229 (друзья/ЛС). Две независимые темы, одна миграция:
--   (1) НАСТРОЙКА «ИИ отвечает в этой переписке» — по паре (я, собеседник);
--   (2) УВЕДОМЛЕНИЯ (браузер/почта/telegram) — по паре (я, тип события).
--
-- РЕШЕНИЯ ПО СХЕМЕ (читать перед правками):
--
-- 1. ``dm_ai_pref`` ключуется по (user_id, peer_id), а НЕ по thread_id.
--    Настройка принадлежит ЧЕЛОВЕКУ и его отношению к КОНКРЕТНОМУ
--    собеседнику: у двух участников одной ветки это две РАЗНЫЕ строки с
--    разными режимами. Ключ по ветке пришлось бы всё равно дополнять
--    «чей это режим», то есть ровно к этой же паре — только через лишний
--    JOIN. Ветки при этом может ещё не быть (настройку можно завести
--    заранее), а pair-ключ этому не мешает.
--
-- 2. Дневная квота ЖИВЁТ В ЭТОЙ ЖЕ СТРОКЕ (``day`` + ``used_today``), а не
--    в отдельной таблице-счётчике и не подсчётом ``dm_message``. Причина:
--    строка и так читается на каждом входящем сообщении (нужен режим), и
--    счётчик обходится нулём дополнительных чтений. Сброс квоты — не крон,
--    а несовпадение ``day`` с сегодняшней датой: читатель, увидев чужой
--    день, трактует ``used_today`` как 0 и перезаписывает при инкременте.
--
-- 3. ``auto_ack`` — ОТДЕЛЬНЫЙ флаг «я понимаю, что ИИ будет писать от моего
--    имени». Он НЕ выводится из ``mode='auto'``: режим можно выставить и
--    программно (миграция, импорт настроек, будущий API), а осознанное
--    согласие человека — это факт, который должен быть записан явно.
--    Резолвер требует ОБА: mode='auto' И auto_ack=1, иначе деградация в
--    draft. То есть «забыли поставить галочку» физически не может привести
--    к тому, что от имени человека уйдёт неподтверждённое сообщение.
--
-- 4. ``dm_ai_draft`` — ЧЕРНОВИК, а не сообщение. Ключ (user_id, thread_id):
--    черновик принадлежит ровно одному человеку и виден ТОЛЬКО ему (его
--    отдаёт лишь /api/messages/{id}/ai под его сессией). В ``dm_message``
--    он не попадает вообще — поэтому poll собеседника его не увидит даже
--    теоретически: там просто нет такой строки. Один черновик на ветку:
--    новое входящее перезаписывает старый (UPSERT), иначе к вечеру в
--    композере копилась бы стопка протухших предложений.
--
-- 5. ``social_notif_pref`` — строка на (человек, событие, канал), а не
--    колонка на канал. Каналы будут добавляться (webpush, матрица, что
--    угодно), и каждый новый канал в широкой таблице — это ALTER TABLE на
--    боевой базе. Здесь это просто новое значение в CHECK. Отсутствие
--    строки = дефолт из кода (браузер ВКЛ, почта и telegram ВЫКЛ), поэтому
--    новому пользователю ничего не надо бэкфиллить.
--
-- 6. ``social_notif_item`` — персональная очередь для браузера. Именно
--    очередь, а не «выбрать всё новое из dm_message/friend_request»:
--    события разнородны (заявка, сообщение, «ИИ ответил за тебя»), и общий
--    источник для них пришлось бы собирать UNION'ом трёх запросов на
--    каждый опрос. ``delivered_at`` — водяной знак ВЫДАЧИ, а не прочтения:
--    показали в браузере → строка больше не всплывает.
--
-- 7. ``social_notif_cooldown`` — «когда этому человеку последний раз ушло
--    письмо про ЭТУ переписку». Ключ (user_id, scope), где scope это
--    'dm:<thread_id>' / 'friend_request' и т.п. Отдельная таблица, а не
--    поле в pref: cooldown — это состояние доставки, а не настройка, и
--    чистка/сброс состояния не должны трогать выбор пользователя.
--
-- ТОКЕН TELEGRAM ЗДЕСЬ НЕ ХРАНИТСЯ. Он лежит в ``user_settings``
-- (ключи ``social_tg_token`` / ``social_tg_chat_id``) — там же, где личный
-- API-ключ LLM участника, с той же изоляцией по PRIMARY KEY(user_id, key).
-- Заводить ради двух строк отдельную таблицу с секретом значило бы
-- размножить места, за которыми нужно следить.
--
-- FK включены глобально (``PRAGMA foreign_keys = ON`` в app/storage/db.py
-- ``_configure_connection``), поэтому ON DELETE CASCADE реально срабатывает:
-- удаление аккаунта уносит его настройки ИИ, черновики и очередь уведомлений.

-- ── (1) «ИИ отвечает за меня в переписке с этим человеком» ──────────────────
CREATE TABLE IF NOT EXISTS dm_ai_pref (
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    peer_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    mode          TEXT    NOT NULL DEFAULT 'off',
    style_note    TEXT    NOT NULL DEFAULT '',
    quota_daily   INTEGER NOT NULL DEFAULT 20,
    used_today    INTEGER NOT NULL DEFAULT 0,
    day           TEXT    NOT NULL DEFAULT '',
    last_reply_at TEXT,
    auto_ack      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, peer_id),
    CHECK (user_id <> peer_id),
    CHECK (mode IN ('off', 'draft', 'auto')),
    CHECK (quota_daily >= 0),
    CHECK (used_today >= 0),
    CHECK (auto_ack IN (0, 1))
);

-- Kill-switch «выключить ИИ во всех переписках» и страница со списком:
-- обе операции — это «все строки ОДНОГО человека».
CREATE INDEX IF NOT EXISTS idx_dm_ai_pref_user
    ON dm_ai_pref(user_id, mode);

-- ── Черновик, который видит ТОЛЬКО его владелец ────────────────────────────
CREATE TABLE IF NOT EXISTS dm_ai_draft (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    thread_id   INTEGER NOT NULL REFERENCES dm_thread(id) ON DELETE CASCADE,
    body        TEXT    NOT NULL,
    reply_to_id INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, thread_id)
);

-- ── (2) Уведомления: настройки по (событие × канал) ────────────────────────
CREATE TABLE IF NOT EXISTS social_notif_pref (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event      TEXT    NOT NULL,
    channel    TEXT    NOT NULL,
    enabled    INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, event, channel),
    CHECK (enabled IN (0, 1)),
    CHECK (channel IN ('browser', 'email', 'telegram')),
    CHECK (event IN ('friend_request', 'friend_accepted', 'dm_message', 'ai_replied'))
);

-- ── Персональная очередь для браузерного опроса ────────────────────────────
CREATE TABLE IF NOT EXISTS social_notif_item (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event        TEXT    NOT NULL,
    title        TEXT    NOT NULL,
    body         TEXT    NOT NULL DEFAULT '',
    url          TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    delivered_at TEXT
);

-- Горячий запрос опроса: «мои ещё не показанные, по возрастанию id».
CREATE INDEX IF NOT EXISTS idx_social_notif_item_pending
    ON social_notif_item(user_id, delivered_at, id);

-- ── Антиспам почты: когда этому человеку последний раз ушло письмо ─────────
CREATE TABLE IF NOT EXISTS social_notif_cooldown (
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    scope        TEXT    NOT NULL,
    last_sent_at TEXT    NOT NULL,
    PRIMARY KEY (user_id, scope)
);
