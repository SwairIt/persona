"""Доступ к данным социального слоя: друзья, заявки, личные сообщения.

Дисциплина авторизации (главное правило файла)
----------------------------------------------
КАЖДАЯ функция принимает id действующего пользователя и сама фильтрует по
нему. Роут НИКОГДА не передаёт «сырой» ``thread_id`` дальше без резолва —
для этого есть ровно ОДИН резолвер :func:`_require_thread_member`, по
образцу ``get_session(user_id, session_id)`` из ``app/chat/sessions.py``.
Резолвер проверяет три вещи сразу:

  1. ветка существует;
  2. пользователь — один из двух её участников;
  3. эти двое ДО СИХ ПОР друзья (переписка только между друзьями).

Любое нарушение → :class:`ThreadAccessError`, роут превращает её в 404 (не
403: «нет такой ветки» не подтверждает существование чужой переписки, то
есть перебор id ничего не сообщает атакующему).

Схема
-----
``friendship`` двунаправленная (две строки на дружбу), ``dm_thread``
канонический (одна строка, ``user_a_id < user_b_id``) — обоснование в
шапке миграции ``229_social_friends_dm.sql``.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

import aiosqlite

from app.auth.owner import is_owner
from app.logging_setup import get_logger
from app.storage.db import get_connection, write_transaction
from app.storage.repository import get_user_kv, set_user_kv

log = get_logger("persona.social")

# Сколько людей максимум отдаём в поиске (анти-перечисление базы).
SEARCH_LIMIT_MAX = 10
# Минимальная длина запроса по имени. Короче — только точный e-mail.
NAME_MIN_CHARS = 3
# Потолок длины сообщения (без него один POST может залить БД).
MAX_MESSAGE_CHARS = 4000

# Ключ настройки «меня можно найти поиском». Живёт в ``user_settings`` для
# ВСЕХ, включая владельца: это ЛИЧНОЕ предпочтение человека, а не настройка
# инстанса (глобальный ``kv_settings`` тут был бы просто неверной семантикой —
# он один на всю установку, а людей в ней много).
DISCOVERABLE_KEY = "social_discoverable"

MessageKind = Literal["human", "ai"]

# Голова SELECT'а поиска людей — вынесена в константу, чтобы сама склейка
# запроса влезала в одну строку (и было видно, что склеиваются литералы).
_SEARCH_HEAD = "SELECT u.id, u.email, u.display_name FROM users u WHERE "


class ThreadAccessError(Exception):
    """Ветка не существует / пользователь не её участник / они не друзья."""


class SocialError(Exception):
    """Осмысленный отказ, который можно показать человеку."""


class UserCard(TypedDict):
    """Публичная карточка человека. E-mail сюда НЕ попадает — только маска."""

    id: int
    name: str
    status: str  # 'none' | 'friends' | 'outgoing' | 'incoming' | 'self'


# ── Общие хелперы ───────────────────────────────────────────────────────────


def _mask_email(email: str) -> str:
    """``yaroslav@gmail.com`` → ``y***@gmail.com``.

    Показываем ПЕРВУЮ букву и домен: этого хватает, чтобы человек узнал
    знакомого, которого сам же ищет по точному адресу, но недостаточно,
    чтобы собрать адрес по кускам.
    """
    raw = (email or "").strip()
    local, sep, domain = raw.partition("@")
    if not sep or not local:
        return "аноним"
    return f"{local[0]}***@{domain}"


def _card_name(display_name: str | None, email: str) -> str:
    """Имя для UI: display_name, иначе маска e-mail. Никогда не сырой адрес."""
    clean = (display_name or "").strip()
    return clean or _mask_email(email)


async def _default_discoverable(user_id: int) -> str:
    """Дефолт флага «меня можно найти»: владелец — ВЫКЛ, участник — ВКЛ.

    Владелец инстанса не должен всплывать в чужом поиске просто потому, что
    он завёл сервер: он «находится» только если сам включил тумблер.

    Сбой резолва роли → ``"0"`` (скрыт). Здесь fail-closed означает именно
    «спрятать»: обратный дефолт («участник, значит виден») при упавшей БД
    выставил бы владельца в чужой поиск — ровно то, чего этот дефолт
    существует, чтобы не допустить. Явно сохранённый тумблер сильнее дефолта,
    так что человек, который сам себя открыл, ничего не теряет.
    """
    try:
        owner = await is_owner(user_id)
    except Exception as exc:  # noqa: BLE001 — сбой резолва не раскрывает людей
        log.warning("social.discoverable_default_failed", error=str(exc))
        return "0"
    return "0" if owner else "1"


async def is_discoverable(user_id: int) -> bool:
    """Виден ли человек в поиске (учитывая роль-зависимый дефолт)."""
    async with get_connection() as conn:
        raw = await get_user_kv(conn, int(user_id), DISCOVERABLE_KEY)
    if raw is None:
        raw = await _default_discoverable(int(user_id))
    return str(raw).strip() == "1"


async def set_discoverable(user_id: int, value: bool) -> None:
    """Сохранить тумблер «меня можно найти по поиску».

    Через ``get_connection``, а не ``write_transaction``: ``set_user_kv``
    коммитит сам, и внутри явной ``BEGIN IMMEDIATE``-транзакции этот коммит
    закрыл бы её раньше времени.
    """
    async with get_connection() as conn:
        await set_user_kv(conn, int(user_id), DISCOVERABLE_KEY, "1" if value else "0")


# ── Поиск людей ─────────────────────────────────────────────────────────────


async def search_users(
    query: str,
    requester_id: int,
    limit: int = SEARCH_LIMIT_MAX,
) -> list[UserCard]:
    """Найти людей по ТОЧНОМУ e-mail или по имени (подстрока, ≥3 символов).

    Правила приватности (все обязательные, менять только вместе с тестами
    в ``tests/test_social.py``):

    * e-mail матчится ТОЛЬКО целиком и без учёта регистра — по ``@gmail.com``
      или ``ya`` базу не перечислить;
    * ``display_name`` матчится подстрокой, но минимум с 3 символов;
    * в ответе НЕТ e-mail — только id, имя (или маска ``y***@gmail.com``)
      и статус дружбы;
    * потолок выдачи — ``SEARCH_LIMIT_MAX``;
    * себя не показываем;
    * не показываем тех, кто ВЫКЛЮЧИЛ ``social_discoverable`` — и это
      сильнее точного e-mail: выключил → ненаходим НИКАК;
    * не показываем тех, кто уже ОТКЛОНИЛ заявку от этого искателя (иначе
      отказ ничего не значит — можно долбиться заново каждый день).
    """
    text = (query or "").strip()
    if not text:
        return []
    safe_limit = max(1, min(int(limit), SEARCH_LIMIT_MAX))
    lowered = text.lower()

    conditions: list[str] = ["u.id <> ?"]
    params: list[Any] = [int(requester_id)]
    match_clauses: list[str] = []
    match_params: list[Any] = []

    if "@" in lowered:
        match_clauses.append("social_lower(u.email) = ?")
        match_params.append(lowered)
    if len(text) >= NAME_MIN_CHARS:
        # ВАЖНО: встроенный ``lower()`` в SQLite умеет только ASCII, поэтому
        # «Бор» не нашёл бы «Борис». Регистронезависимость по-настоящему даёт
        # только питоновский ``str.lower`` — регистрируем его как функцию
        # соединения (см. ``_register_lower``).
        match_clauses.append(
            "(u.display_name IS NOT NULL "
            "AND social_lower(u.display_name) LIKE ? ESCAPE '\\')"
        )
        match_params.append(f"%{_like_escape(lowered)}%")
    if not match_clauses:
        # Слишком короткий запрос и не e-mail — молчим (а не отдаём всех).
        return []
    conditions.append("(" + " OR ".join(match_clauses) + ")")
    params.extend(match_params)

    # Кто уже отклонил заявку ИМЕННО от этого искателя — вычёркиваем.
    conditions.append(
        "u.id NOT IN ("
        " SELECT to_user_id FROM friend_request"
        " WHERE from_user_id = ? AND status = 'declined')"
    )
    params.append(int(requester_id))

    # Склейка безопасна: в строку попадают ТОЛЬКО литералы из ``conditions``
    # (собраны выше в этой же функции), всё пользовательское уходит
    # плейсхолдерами в ``params``.
    sql = _SEARCH_HEAD + " AND ".join(conditions) + " ORDER BY u.id LIMIT ?"
    # Берём с запасом: часть строк отсеет фильтр видимости ниже.
    params.append(safe_limit * 4)

    async with get_connection() as conn:
        await _register_lower(conn)
        cursor = await conn.execute(sql, params)
        rows = await cursor.fetchall()
        if not rows:
            return []
        ids = [int(r["id"]) for r in rows]
        visible = await _visible_ids(conn, ids)
        statuses = await _relation_map(conn, int(requester_id), ids)

    cards: list[UserCard] = []
    for row in rows:
        uid = int(row["id"])
        if uid not in visible:
            continue
        cards.append(
            {
                "id": uid,
                "name": _card_name(row["display_name"], str(row["email"])),
                "status": statuses.get(uid, "none"),
            }
        )
        if len(cards) >= safe_limit:
            break
    return cards


def _py_lower(value: str | None) -> str | None:
    """Unicode-строчные для SQLite (встроенный ``lower()`` — только ASCII)."""
    return value.lower() if isinstance(value, str) else None


async def _register_lower(conn: aiosqlite.Connection) -> None:
    """Повесить ``social_lower`` на ЭТО соединение (не глобально)."""
    await conn.create_function("social_lower", 1, _py_lower, deterministic=True)


def _like_escape(text: str) -> str:
    """Экранируем LIKE-джокеры: ``%`` в запросе не должен матчить всех."""
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


async def _visible_ids(conn: aiosqlite.Connection, ids: list[int]) -> set[int]:
    """Из списка id оставить тех, у кого ``social_discoverable`` включён."""
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    cursor = await conn.execute(
        f"SELECT user_id, value FROM user_settings "  # noqa: S608 — только плейсхолдеры
        f"WHERE key = ? AND user_id IN ({placeholders})",
        [DISCOVERABLE_KEY, *ids],
    )
    explicit = {int(r["user_id"]): str(r["value"]).strip() for r in await cursor.fetchall()}
    visible: set[int] = set()
    for uid in ids:
        value = explicit.get(uid)
        if value is None:
            value = await _default_discoverable(uid)
        if value == "1":
            visible.add(uid)
    return visible


async def _relation_map(
    conn: aiosqlite.Connection, requester_id: int, ids: list[int]
) -> dict[int, str]:
    """Статус отношений искателя с каждым из ``ids``."""
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    result: dict[int, str] = {}

    cursor = await conn.execute(
        f"SELECT friend_id FROM friendship "  # noqa: S608 — только плейсхолдеры
        f"WHERE user_id = ? AND friend_id IN ({placeholders})",
        [requester_id, *ids],
    )
    for row in await cursor.fetchall():
        result[int(row["friend_id"])] = "friends"

    cursor = await conn.execute(
        f"SELECT from_user_id, to_user_id FROM friend_request "  # noqa: S608
        f"WHERE status = 'pending' AND ("
        f"  (from_user_id = ? AND to_user_id IN ({placeholders}))"
        f"  OR (to_user_id = ? AND from_user_id IN ({placeholders})))",
        [requester_id, *ids, requester_id, *ids],
    )
    for row in await cursor.fetchall():
        if int(row["from_user_id"]) == requester_id:
            result.setdefault(int(row["to_user_id"]), "outgoing")
        else:
            result.setdefault(int(row["from_user_id"]), "incoming")
    return result


# ── Заявки в друзья ─────────────────────────────────────────────────────────


async def send_request(
    from_user_id: int, to_user_id: int, message: str = ""
) -> int:
    """Отправить (или переотправить) заявку. Возвращает id заявки.

    Повторная заявка после ``declined``/``cancelled`` не создаёт вторую
    строку, а ПЕРЕВОДИТ существующую обратно в ``pending`` (UNIQUE на пару).

    Особый случай: если встречная заявка от адресата уже висит в
    ``pending`` — это взаимность, сразу принимаем её (дружба + ветка), а не
    плодим вторую заявку навстречу.
    """
    sender = int(from_user_id)
    target = int(to_user_id)
    if sender == target:
        raise SocialError("нельзя добавить в друзья самого себя")
    note = (message or "").strip()[:280]

    async with write_transaction() as conn:
        cursor = await conn.execute("SELECT id FROM users WHERE id = ?", (target,))
        if await cursor.fetchone() is None:
            raise SocialError("такого пользователя нет")
        # Ненаходимый = недоступный вообще: и по поиску, и по прямому id.
        if not await _discoverable_in(conn, target):
            raise SocialError("такого пользователя нет")
        if await _are_friends(conn, sender, target):
            raise SocialError("вы уже друзья")

        cursor = await conn.execute(
            "SELECT id FROM friend_request "
            "WHERE from_user_id = ? AND to_user_id = ? AND status = 'pending'",
            (target, sender),
        )
        incoming = await cursor.fetchone()
        if incoming is not None:
            await _accept(conn, int(incoming["id"]), sender)
            return int(incoming["id"])

        cursor = await conn.execute(
            """
            INSERT INTO friend_request
                (from_user_id, to_user_id, status, message, created_at, responded_at)
            VALUES (?, ?, 'pending', ?, datetime('now'), NULL)
            ON CONFLICT(from_user_id, to_user_id) DO UPDATE SET
                status = 'pending',
                message = excluded.message,
                created_at = datetime('now'),
                responded_at = NULL
            """,
            (sender, target, note or None),
        )
        _ = cursor
        cursor = await conn.execute(
            "SELECT id FROM friend_request WHERE from_user_id = ? AND to_user_id = ?",
            (sender, target),
        )
        row = await cursor.fetchone()
    return int(row["id"]) if row is not None else 0


async def _discoverable_in(conn: aiosqlite.Connection, user_id: int) -> bool:
    cursor = await conn.execute(
        "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
        (int(user_id), DISCOVERABLE_KEY),
    )
    row = await cursor.fetchone()
    if row is not None:
        raw = str(row["value"]).strip()
    else:
        raw = await _default_discoverable(int(user_id))
    return raw == "1"


async def _accept(conn: aiosqlite.Connection, request_id: int, user_id: int) -> bool:
    """Принять входящую заявку. Только адресат, только ``pending``."""
    cursor = await conn.execute(
        "SELECT from_user_id, to_user_id FROM friend_request "
        "WHERE id = ? AND to_user_id = ? AND status = 'pending'",
        (int(request_id), int(user_id)),
    )
    row = await cursor.fetchone()
    if row is None:
        return False
    a, b = int(row["from_user_id"]), int(row["to_user_id"])
    await conn.execute(
        "UPDATE friend_request SET status = 'accepted', responded_at = datetime('now') "
        "WHERE id = ?",
        (int(request_id),),
    )
    # Двунаправленно: обе строки разом (см. шапку миграции 229).
    await conn.execute(
        "INSERT OR IGNORE INTO friendship (user_id, friend_id, created_at) "
        "VALUES (?, ?, datetime('now')), (?, ?, datetime('now'))",
        (a, b, b, a),
    )
    return True


async def accept_request(request_id: int, user_id: int) -> bool:
    """Принять заявку (может только её адресат)."""
    async with write_transaction() as conn:
        return await _accept(conn, int(request_id), int(user_id))


async def decline_request(request_id: int, user_id: int) -> bool:
    """Отклонить входящую заявку (может только адресат)."""
    async with write_transaction() as conn:
        cursor = await conn.execute(
            "UPDATE friend_request SET status = 'declined', responded_at = datetime('now') "
            "WHERE id = ? AND to_user_id = ? AND status = 'pending'",
            (int(request_id), int(user_id)),
        )
        return cursor.rowcount > 0


async def cancel_request(request_id: int, user_id: int) -> bool:
    """Отменить СВОЮ исходящую заявку (может только отправитель)."""
    async with write_transaction() as conn:
        cursor = await conn.execute(
            "UPDATE friend_request SET status = 'cancelled', responded_at = datetime('now') "
            "WHERE id = ? AND from_user_id = ? AND status = 'pending'",
            (int(request_id), int(user_id)),
        )
        return cursor.rowcount > 0


async def list_incoming(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """Входящие заявки, ожидающие моего ответа."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT r.id, r.message, r.created_at, u.id AS uid, u.email, u.display_name "
            "FROM friend_request r JOIN users u ON u.id = r.from_user_id "
            "WHERE r.to_user_id = ? AND r.status = 'pending' "
            "ORDER BY r.created_at DESC LIMIT ?",
            (int(user_id), max(1, min(int(limit), 200))),
        )
        rows = await cursor.fetchall()
    return [_request_row(row) for row in rows]


async def list_outgoing(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """Мои исходящие заявки, на которые ещё не ответили."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT r.id, r.message, r.created_at, u.id AS uid, u.email, u.display_name "
            "FROM friend_request r JOIN users u ON u.id = r.to_user_id "
            "WHERE r.from_user_id = ? AND r.status = 'pending' "
            "ORDER BY r.created_at DESC LIMIT ?",
            (int(user_id), max(1, min(int(limit), 200))),
        )
        rows = await cursor.fetchall()
    return [_request_row(row) for row in rows]


def _request_row(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "user_id": int(row["uid"]),
        "name": _card_name(row["display_name"], str(row["email"])),
        "message": str(row["message"] or ""),
        "created_at": str(row["created_at"] or ""),
    }


# ── Друзья ──────────────────────────────────────────────────────────────────


async def _are_friends(conn: aiosqlite.Connection, a: int, b: int) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM friendship WHERE user_id = ? AND friend_id = ?",
        (int(a), int(b)),
    )
    return await cursor.fetchone() is not None


async def are_friends(user_id: int, other_id: int) -> bool:
    """Дружим ли мы (двунаправленная схема → одна выборка)."""
    async with get_connection() as conn:
        return await _are_friends(conn, int(user_id), int(other_id))


async def list_friends(user_id: int, limit: int = 500) -> list[dict[str, Any]]:
    """Мои друзья + id общей ветки переписки, если она уже заведена."""
    uid = int(user_id)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT u.id AS uid, u.email, u.display_name, f.created_at, "
            "       t.id AS thread_id "
            "FROM friendship f "
            "JOIN users u ON u.id = f.friend_id "
            "LEFT JOIN dm_thread t ON t.user_a_id = MIN(f.user_id, f.friend_id) "
            "                     AND t.user_b_id = MAX(f.user_id, f.friend_id) "
            "WHERE f.user_id = ? "
            "ORDER BY lower(COALESCE(u.display_name, u.email)) LIMIT ?",
            (uid, max(1, min(int(limit), 1000))),
        )
        rows = await cursor.fetchall()
    return [
        {
            "id": int(row["uid"]),
            "name": _card_name(row["display_name"], str(row["email"])),
            "since": str(row["created_at"] or ""),
            "thread_id": int(row["thread_id"]) if row["thread_id"] is not None else None,
        }
        for row in rows
    ]


async def unfriend(user_id: int, friend_id: int) -> bool:
    """Удалить дружбу. Двунаправленная схема → сносим ОБЕ строки.

    Ветку переписки и её сообщения НЕ удаляем: история остаётся у обоих, но
    писать в неё больше нельзя (:func:`_require_thread_member` проверяет
    дружбу на каждом обращении). Заявки переводим в ``cancelled``, чтобы
    можно было позвать заново.
    """
    a, b = int(user_id), int(friend_id)
    async with write_transaction() as conn:
        cursor = await conn.execute(
            "DELETE FROM friendship WHERE (user_id = ? AND friend_id = ?) "
            "OR (user_id = ? AND friend_id = ?)",
            (a, b, b, a),
        )
        removed = cursor.rowcount > 0
        if removed:
            await conn.execute(
                "UPDATE friend_request SET status = 'cancelled', "
                "responded_at = datetime('now') "
                "WHERE status = 'accepted' AND ((from_user_id = ? AND to_user_id = ?) "
                "OR (from_user_id = ? AND to_user_id = ?))",
                (a, b, b, a),
            )
    return removed


# ── Ветки переписки ─────────────────────────────────────────────────────────


def _pair(a: int, b: int) -> tuple[int, int]:
    """Канонический порядок пары для ``dm_thread`` (a < b)."""
    return (a, b) if a < b else (b, a)


async def _require_thread_member(
    conn: aiosqlite.Connection, thread_id: int, user_id: int
) -> int:
    """ЕДИНСТВЕННЫЙ резолвер доступа к ветке. Возвращает id собеседника.

    Проверяет разом: ветка есть → я её участник → мы всё ещё друзья.
    Любой промах — :class:`ThreadAccessError` (роут отдаёт 404, чтобы
    перебор id не подтверждал существование чужой переписки).
    """
    uid = int(user_id)
    cursor = await conn.execute(
        "SELECT user_a_id, user_b_id FROM dm_thread WHERE id = ?",
        (int(thread_id),),
    )
    row = await cursor.fetchone()
    if row is None:
        raise ThreadAccessError("ветки нет")
    a, b = int(row["user_a_id"]), int(row["user_b_id"])
    if uid not in (a, b):
        raise ThreadAccessError("не участник ветки")
    other = b if uid == a else a
    if not await _are_friends(conn, uid, other):
        raise ThreadAccessError("переписка только между друзьями")
    return other


async def get_or_create_thread(user_id: int, other_id: int) -> int:
    """Открыть (или завести) ветку с ДРУГОМ. Не с другом — отказ."""
    uid, other = int(user_id), int(other_id)
    if uid == other:
        raise ThreadAccessError("нельзя написать самому себе")
    async with write_transaction() as conn:
        if not await _are_friends(conn, uid, other):
            raise ThreadAccessError("переписка только между друзьями")
        a, b = _pair(uid, other)
        await conn.execute(
            "INSERT OR IGNORE INTO dm_thread (user_a_id, user_b_id, created_at) "
            "VALUES (?, ?, datetime('now'))",
            (a, b),
        )
        cursor = await conn.execute(
            "SELECT id FROM dm_thread WHERE user_a_id = ? AND user_b_id = ?",
            (a, b),
        )
        row = await cursor.fetchone()
    if row is None:  # pragma: no cover — INSERT OR IGNORE только что отработал
        raise ThreadAccessError("не удалось открыть переписку")
    return int(row["id"])


async def list_threads(user_id: int, limit: int = 100) -> list[dict[str, Any]]:
    """Мои переписки: собеседник, последнее сообщение, непрочитанное.

    Ветки с уже удалёнными из друзей людьми в список не попадают — иначе
    страница показывала бы то, что открыть всё равно нельзя.
    """
    uid = int(user_id)
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT t.id AS thread_id,
                   t.last_message_at,
                   u.id AS other_id, u.email, u.display_name,
                   (SELECT m.body FROM dm_message m
                     WHERE m.thread_id = t.id ORDER BY m.id DESC LIMIT 1) AS last_body,
                   (SELECT m.kind FROM dm_message m
                     WHERE m.thread_id = t.id ORDER BY m.id DESC LIMIT 1) AS last_kind,
                   (SELECT m.sender_id FROM dm_message m
                     WHERE m.thread_id = t.id ORDER BY m.id DESC LIMIT 1) AS last_sender,
                   (SELECT COUNT(*) FROM dm_message m
                     WHERE m.thread_id = t.id AND m.sender_id <> ?
                       AND m.read_at IS NULL) AS unread
              FROM dm_thread t
              JOIN users u
                ON u.id = CASE WHEN t.user_a_id = ? THEN t.user_b_id ELSE t.user_a_id END
              JOIN friendship f
                ON f.user_id = ? AND f.friend_id = u.id
             WHERE t.user_a_id = ? OR t.user_b_id = ?
             ORDER BY COALESCE(t.last_message_at, t.created_at) DESC
             LIMIT ?
            """,
            (uid, uid, uid, uid, uid, max(1, min(int(limit), 500))),
        )
        rows = await cursor.fetchall()
    return [
        {
            "thread_id": int(row["thread_id"]),
            "other_id": int(row["other_id"]),
            "name": _card_name(row["display_name"], str(row["email"])),
            "last_body": str(row["last_body"] or ""),
            "last_kind": str(row["last_kind"] or "human"),
            "last_mine": row["last_sender"] is not None and int(row["last_sender"]) == uid,
            "last_message_at": str(row["last_message_at"] or ""),
            "unread": int(row["unread"] or 0),
        }
        for row in rows
    ]


async def thread_header(thread_id: int, user_id: int) -> dict[str, Any]:
    """Шапка переписки (кто собеседник). Резолв доступа внутри."""
    async with get_connection() as conn:
        other = await _require_thread_member(conn, int(thread_id), int(user_id))
        cursor = await conn.execute(
            "SELECT id, email, display_name FROM users WHERE id = ?", (other,)
        )
        row = await cursor.fetchone()
    if row is None:  # pragma: no cover — FK не даст осиротеть
        raise ThreadAccessError("собеседник удалён")
    return {
        "thread_id": int(thread_id),
        "other_id": int(row["id"]),
        "name": _card_name(row["display_name"], str(row["email"])),
    }


# ── Сообщения ───────────────────────────────────────────────────────────────


def _message_row(row: aiosqlite.Row, uid: int) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "body": str(row["body"]),
        "kind": str(row["kind"] or "human"),
        "created_at": str(row["created_at"] or ""),
        "mine": int(row["sender_id"]) == uid,
        "sender_id": int(row["sender_id"]),
        "read": row["read_at"] is not None,
    }


async def list_messages(
    thread_id: int,
    user_id: int,
    before_id: int | None = None,
    limit: int = 50,
    after_id: int | None = None,
) -> list[dict[str, Any]]:
    """Сообщения ветки в хронологическом порядке.

    ``before_id`` — страница «старее» (кнопка «показать раньше»);
    ``after_id`` — только новые (poll). Доступ резолвится ДО выборки.
    """
    uid = int(user_id)
    safe_limit = max(1, min(int(limit), 200))
    async with get_connection() as conn:
        await _require_thread_member(conn, int(thread_id), uid)
        if after_id is not None:
            cursor = await conn.execute(
                "SELECT id, body, kind, created_at, sender_id, read_at FROM dm_message "
                "WHERE thread_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
                (int(thread_id), int(after_id), safe_limit),
            )
            rows = list(await cursor.fetchall())
        else:
            sql = (
                "SELECT id, body, kind, created_at, sender_id, read_at "
                "FROM dm_message WHERE thread_id = ?"
            )
            params: list[Any] = [int(thread_id)]
            if before_id is not None:
                sql += " AND id < ?"
                params.append(int(before_id))
            sql += " ORDER BY id DESC LIMIT ?"
            params.append(safe_limit)
            cursor = await conn.execute(sql, params)
            rows = list(reversed(await cursor.fetchall()))
    return [_message_row(row, uid) for row in rows]


async def send_message(
    thread_id: int,
    sender_id: int,
    body: str,
    kind: MessageKind = "human",
) -> dict[str, Any]:
    """Отправить сообщение в ветку. Доступ резолвится ДО вставки.

    ``kind='ai'`` — ответ, который ИИ написал ОТ ИМЕНИ отправителя
    (автоответ появится следующим срезом; колонка и метка «✨ ответил ИИ»
    существуют уже сейчас).
    """
    uid = int(sender_id)
    text = (body or "").strip()
    if not text:
        raise SocialError("пустое сообщение")
    text = text[:MAX_MESSAGE_CHARS]
    safe_kind: MessageKind = "ai" if str(kind) == "ai" else "human"

    async with write_transaction() as conn:
        await _require_thread_member(conn, int(thread_id), uid)
        cursor = await conn.execute(
            "INSERT INTO dm_message (thread_id, sender_id, body, created_at, kind) "
            "VALUES (?, ?, ?, datetime('now'), ?)",
            (int(thread_id), uid, text, safe_kind),
        )
        message_id = int(cursor.lastrowid or 0)
        await conn.execute(
            "UPDATE dm_thread SET last_message_at = datetime('now') WHERE id = ?",
            (int(thread_id),),
        )
        cursor = await conn.execute(
            "SELECT id, body, kind, created_at, sender_id, read_at FROM dm_message "
            "WHERE id = ?",
            (message_id,),
        )
        row = await cursor.fetchone()
    if row is None:  # pragma: no cover
        raise SocialError("сообщение не сохранилось")
    return _message_row(row, uid)


async def mark_read(thread_id: int, user_id: int) -> int:
    """Отметить ЧУЖИЕ сообщения ветки прочитанными. Возвращает сколько."""
    uid = int(user_id)
    async with write_transaction() as conn:
        await _require_thread_member(conn, int(thread_id), uid)
        cursor = await conn.execute(
            "UPDATE dm_message SET read_at = datetime('now') "
            "WHERE thread_id = ? AND sender_id <> ? AND read_at IS NULL",
            (int(thread_id), uid),
        )
        return int(cursor.rowcount or 0)


async def unread_total(user_id: int) -> int:
    """Сколько непрочитанных сообщений у меня во ВСЕХ ветках (для бейджа).

    Считаем только ветки с действующими друзьями — ровно те, что видны
    на ``/messages``.
    """
    uid = int(user_id)
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT COUNT(*) AS n
              FROM dm_message m
              JOIN dm_thread t ON t.id = m.thread_id
              JOIN friendship f
                ON f.user_id = ?
               AND f.friend_id = CASE WHEN t.user_a_id = ?
                                      THEN t.user_b_id ELSE t.user_a_id END
             WHERE (t.user_a_id = ? OR t.user_b_id = ?)
               AND m.sender_id <> ?
               AND m.read_at IS NULL
            """,
            (uid, uid, uid, uid, uid),
        )
        row = await cursor.fetchone()
    return int(row["n"] or 0) if row is not None else 0


__all__ = [
    "DISCOVERABLE_KEY",
    "MAX_MESSAGE_CHARS",
    "NAME_MIN_CHARS",
    "SEARCH_LIMIT_MAX",
    "SocialError",
    "ThreadAccessError",
    "UserCard",
    "accept_request",
    "are_friends",
    "cancel_request",
    "decline_request",
    "get_or_create_thread",
    "is_discoverable",
    "list_friends",
    "list_incoming",
    "list_messages",
    "list_outgoing",
    "list_threads",
    "mark_read",
    "search_users",
    "send_message",
    "send_request",
    "set_discoverable",
    "thread_header",
    "unfriend",
    "unread_total",
]
