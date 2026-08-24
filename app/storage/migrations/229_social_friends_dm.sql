-- Социальный слой: друзья + личные сообщения между зарегистрированными
-- пользователями (v2.31.x, срез 1).
--
-- РЕШЕНИЕ ПО СХЕМЕ (важно, читать перед правками):
--
-- 1. ``friendship`` хранит ОБА направления. На принятие заявки пишутся ДВЕ
--    строки: (a,b) и (b,a). Это чуть дороже по месту (две строки на дружбу),
--    зато КАЖДЫЙ запрос «мои друзья» / «дружим ли мы» — это одна тривиальная
--    выборка ``WHERE user_id = ?`` без OR/CASE/min-max-акробатики. В коде,
--    где авторизация делается на каждом чтении, простота запроса = меньше
--    шансов на дырку. Удаление из друзей ОБЯЗАНО удалять обе строки
--    (см. ``unfriend`` в app/social/repository.py).
--
-- 2. ``dm_thread`` наоборот хранит ОДНУ строку с КАНОНИЧЕСКИМ порядком
--    ``user_a_id < user_b_id`` (CHECK это гарантирует на уровне БД). Так
--    UNIQUE(user_a_id, user_b_id) физически запрещает две ветки на одну пару,
--    а ``get_or_create_thread`` просто сортирует пару перед вставкой.
--
-- 3. ``dm_message.kind`` — 'human' | 'ai'. 'ai' = ответ, который ИИ написал
--    ОТ ИМЕНИ пользователя (автоответ придёт следующим срезом), но колонка и
--    её отрисовка («✨ ответил ИИ») существуют уже сейчас, чтобы не мигрировать
--    сообщения задним числом.
--
-- FK включены глобально (``PRAGMA foreign_keys = ON`` в app/storage/db.py
-- ``_configure_connection``), поэтому ON DELETE CASCADE реально срабатывает:
-- удаление аккаунта уносит его дружбы, заявки и ветки переписки.

-- ── Дружба (двунаправленная, две строки на пару) ────────────────────────────
CREATE TABLE IF NOT EXISTS friendship (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    friend_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, friend_id),
    CHECK (user_id <> friend_id)
);

-- «Кто добавил меня» — нужен для чистого удаления обеих строк и для
-- обратных проверок.
CREATE INDEX IF NOT EXISTS idx_friendship_friend
    ON friendship(friend_id);

-- ── Заявки в друзья ─────────────────────────────────────────────────────────
-- UNIQUE(from_user_id, to_user_id): на пару «отправитель → получатель» живёт
-- РОВНО ОДНА строка. Повторная заявка после отказа/отмены не вставляет вторую,
-- а ПЕРЕЗАПИСЫВАЕТ существующую в status='pending' (UPSERT в send_request) —
-- иначе после единственного «отклонить» человек навсегда терял возможность
-- написать снова.
CREATE TABLE IF NOT EXISTS friend_request (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    to_user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    status       TEXT    NOT NULL DEFAULT 'pending',
    message      TEXT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    responded_at TEXT,
    UNIQUE (from_user_id, to_user_id),
    CHECK (from_user_id <> to_user_id),
    CHECK (status IN ('pending', 'accepted', 'declined', 'cancelled'))
);

-- «Входящие ко мне, ожидающие ответа» — самый горячий запрос страницы /friends.
CREATE INDEX IF NOT EXISTS idx_friend_request_incoming
    ON friend_request(to_user_id, status);
CREATE INDEX IF NOT EXISTS idx_friend_request_outgoing
    ON friend_request(from_user_id, status);

-- ── Ветка переписки (одна строка на пару, канонический порядок a<b) ─────────
CREATE TABLE IF NOT EXISTS dm_thread (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_a_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    user_b_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    last_message_at TEXT,
    UNIQUE (user_a_id, user_b_id),
    CHECK (user_a_id < user_b_id)
);

-- Список веток пользователя: он может стоять в любой из двух колонок,
-- поэтому по индексу на каждую.
CREATE INDEX IF NOT EXISTS idx_dm_thread_user_a
    ON dm_thread(user_a_id, last_message_at DESC);
CREATE INDEX IF NOT EXISTS idx_dm_thread_user_b
    ON dm_thread(user_b_id, last_message_at DESC);

-- ── Сообщения ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS dm_message (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id  INTEGER NOT NULL REFERENCES dm_thread(id) ON DELETE CASCADE,
    sender_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body       TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    read_at    TEXT,
    kind       TEXT    NOT NULL DEFAULT 'human',
    CHECK (kind IN ('human', 'ai'))
);

-- Пагинация внутри ветки (ORDER BY id) и poll «что нового после id».
CREATE INDEX IF NOT EXISTS idx_dm_message_thread
    ON dm_message(thread_id, id);

-- Счётчик непрочитанного: «в этой ветке НЕ мои и read_at IS NULL».
CREATE INDEX IF NOT EXISTS idx_dm_message_unread
    ON dm_message(thread_id, sender_id, read_at);
