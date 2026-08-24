"""«Одолжить свою модель другу» — выдачи доступа (``llm_grant``) и квоты.

Модель поведения
----------------
Участник по умолчанию платит за себя: свой провайдер и свой ключ в
``user_settings`` (см. ``_resolve_user_provider_and_key`` в
:mod:`app.llm.client`). Этот модуль добавляет ЕДИНСТВЕННОЕ исключение —
ЯВНУЮ, ПОИМЁННУЮ выдачу: человек А разрешает человеку Б ходить в СВОЮ модель
не более N раз в сутки. Ни «всем друзьям», ни «по умолчанию», ни «безлимитно».

Ключ выдающего НИКОГДА не показывается получателю: он читается на сервере
внутри :func:`app.llm.client.make_client` и живёт только в памяти процесса.
Ни один запрос этого модуля не возвращает значение ключа наружу — функции
отдают провайдера и лимиты, но не креденшелы.

Квота
-----
Счётчик — отдельная таблица ``llm_grant_usage(grant_id, day, used)``, а не
подсчёт строк ``llm_usage``. Так проверка лимита это чтение одной строки по
PK, инкремент+проверка выражаются ОДНИМ SQL-стейтментом (значит две
параллельные вкладки не могут вместе пробить лимит), и квота не зависит от
журнала расхода, запись в который намеренно best-effort.

Дружба
------
Выдача честна только между подтверждёнными друзьями — НО таблица
``friendship`` приезжает миграцией 229 из соседнего среза, и модуль
``app.social.*`` здесь НЕ импортируется намеренно (его может не быть).
Проверка сделана «оборонительно»: сырой запрос в ``friendship``, а отсутствие
таблицы трактуется как «друзья» (выдача и так поимённая и явная).
"""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

from app.logging_setup import get_logger
from app.storage.db import get_connection, write_transaction

if TYPE_CHECKING:  # pragma: no cover - только для аннотаций
    import aiosqlite

log = get_logger("persona.llm.grants")

#: Дневной лимит по умолчанию для новой выдачи. Осознанно скромный: это
#: чужие деньги, и «щедрый дефолт» здесь — способ незаметно подарить чужой
#: кошелёк. Поднять до любого значения можно вручную в форме.
DEFAULT_DAILY_LIMIT = 50

#: Верхняя граница поля (защита от опечатки «5000000» и от переполнения UI).
MAX_DAILY_LIMIT = 100_000

# ─── ТОЧКА УЖЕСТОЧЕНИЯ ДРУЖБЫ ───────────────────────────────────────────────
# TODO(social-229): когда социальный слой станет стабильным (миграция 229 +
# app/social/repository.py с публичным ``are_friends``), заменить сырой запрос
# в ``_friends_ok`` ниже на вызов их API и убрать этот флаг. Конкретная строка
# для правки — ``SELECT 1 FROM friendship WHERE user_id = ? AND friend_id = ?``
# внутри :func:`_friends_ok`. Флаг оставлен потому, что резолвер LLM не имеет
# права падать из-за чужого, ещё не приземлившегося модуля.
REQUIRE_FRIENDSHIP = True


def _today() -> str:
    """Локальная дата сервера как ``YYYY-MM-DD`` (ключ дневной квоты).

    Отдельная функция, а не инлайн ``date.today()``, ровно ради тестов: сброс
    квоты «на следующий день» проверяется подменой этой функции, а не сном.
    """
    return date.today().isoformat()


def _row(row: Any) -> dict[str, Any]:
    # ``.keys()`` здесь обязателен, а не «лишний»: итерация по самому
    # ``aiosqlite.Row`` отдаёт ЗНАЧЕНИЯ, а не имена колонок.
    return {k: row[k] for k in row.keys()}  # noqa: SIM118


# ---------------------------------------------------------------------------
# Дружба (оборонительно — таблица может не существовать)
# ---------------------------------------------------------------------------


async def _friends_ok(
    conn: aiosqlite.Connection, grantor_id: int, grantee_id: int
) -> bool:
    """Подтверждена ли дружба. Нет таблицы ``friendship`` → считаем, что да.

    ``friendship`` хранит ОБА направления (миграция 229), поэтому достаточно
    одной проверки в одну сторону.
    """
    if not REQUIRE_FRIENDSHIP:
        return True
    try:
        cursor = await conn.execute(
            "SELECT 1 FROM friendship WHERE user_id = ? AND friend_id = ? LIMIT 1",
            (grantor_id, grantee_id),
        )
        row = await cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 — миграция 229 могла не приземлиться
        log.debug("llm_grant.friendship_table_missing", error=str(exc))
        return True
    return row is not None


async def friends_confirmed(grantor_id: int, grantee_id: int) -> bool:
    """Публичная обёртка :func:`_friends_ok` со своим соединением (для UI)."""
    async with get_connection() as conn:
        return await _friends_ok(conn, int(grantor_id), int(grantee_id))


# ---------------------------------------------------------------------------
# Чтение
# ---------------------------------------------------------------------------


_ACTIVE_SQL = """
SELECT g.id, g.grantor_id, g.grantee_id, g.daily_limit, g.note, g.created_at,
       u.email AS grantor_email
  FROM llm_grant AS g
  JOIN users AS u ON u.id = g.grantor_id
 WHERE g.grantee_id = ?
   AND g.enabled = 1
   AND g.revoked_at IS NULL
 ORDER BY g.created_at ASC, g.id ASC
"""


async def active_grants_for(grantee_id: int) -> list[dict[str, Any]]:
    """Живые выдачи, полученные пользователем (дружба уже проверена).

    Квота НЕ проверяется и НЕ тратится — этим занимается
    :func:`consume_quota`. Разделение нужно, чтобы «настроен ли у человека
    AI» (баннер, бейдж) можно было спросить, не сжигая запрос.
    """
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(_ACTIVE_SQL, (int(grantee_id),))
            rows = [_row(r) for r in await cursor.fetchall()]
            out: list[dict[str, Any]] = []
            for row in rows:
                if await _friends_ok(conn, int(row["grantor_id"]), int(grantee_id)):
                    out.append(row)
    except Exception as exc:  # noqa: BLE001 — старая БД без миграции 230
        log.debug("llm_grant.lookup_failed", error=str(exc))
        return []
    return out


async def usage_today(grant_id: int, day: str | None = None) -> int:
    """Сколько запросов по этой выдаче уже потрачено сегодня."""
    target = day or _today()
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT used FROM llm_grant_usage WHERE grant_id = ? AND day = ?",
                (int(grant_id), target),
            )
            row = await cursor.fetchone()
    except Exception as exc:  # noqa: BLE001
        log.debug("llm_grant.usage_read_failed", error=str(exc))
        return 0
    return int(row["used"]) if row is not None else 0


async def list_issued_by(grantor_id: int) -> list[dict[str, Any]]:
    """«Я делюсь» — все НЕ отозванные выдачи пользователя + расход за сегодня."""
    day = _today()
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT g.id, g.grantee_id, g.daily_limit, g.enabled, g.note,
                   g.created_at, u.email AS grantee_email,
                   COALESCE(usg.used, 0) AS used_today
              FROM llm_grant AS g
              JOIN users AS u ON u.id = g.grantee_id
              LEFT JOIN llm_grant_usage AS usg
                     ON usg.grant_id = g.id AND usg.day = ?
             WHERE g.grantor_id = ? AND g.revoked_at IS NULL
             ORDER BY g.created_at DESC, g.id DESC
            """,
            (day, int(grantor_id)),
        )
        rows = [_row(r) for r in await cursor.fetchall()]
        for row in rows:
            row["friends"] = await _friends_ok(
                conn, int(grantor_id), int(row["grantee_id"])
            )
    return rows


async def list_received_by(grantee_id: int) -> list[dict[str, Any]]:
    """«Мне дали доступ» — выдачи в мою сторону + расход и провайдер выдавшего.

    Провайдер отдаётся ТОЛЬКО как имя (``openrouter``, ``ollama``…): ключ не
    читается вовсе, чтобы физически не было пути, по которому он утечёт в
    шаблон или в JSON.
    """
    day = _today()
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT g.id, g.grantor_id, g.daily_limit, g.enabled, g.note,
                   g.created_at, u.email AS grantor_email,
                   COALESCE(usg.used, 0) AS used_today
              FROM llm_grant AS g
              JOIN users AS u ON u.id = g.grantor_id
              LEFT JOIN llm_grant_usage AS usg
                     ON usg.grant_id = g.id AND usg.day = ?
             WHERE g.grantee_id = ? AND g.revoked_at IS NULL
             ORDER BY g.created_at DESC, g.id DESC
            """,
            (day, int(grantee_id)),
        )
        rows = [_row(r) for r in await cursor.fetchall()]
        for row in rows:
            row["friends"] = await _friends_ok(
                conn, int(row["grantor_id"]), int(grantee_id)
            )
            row["provider"] = await grantor_provider_name(int(row["grantor_id"]))
    return rows


async def find_user_by_email(email: str) -> dict[str, Any] | None:
    """Аккаунт по ТОЧНОМУ email (регистронезависимо), либо ``None``.

    Живёт здесь, а не в роуте: ``tests/test_architecture_gates.py`` запрещает
    новым роутам держать SQL и соединение с БД у себя — вся работа с хранилищем
    прячется за адаптер. Поиска по подстроке нет намеренно: это форма выдачи
    доступа к чужому кошельку, и «похожий» человек — не тот человек.
    """
    clean = (email or "").strip().lower()
    if not clean or "@" not in clean:
        return None
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT id, email FROM users WHERE lower(email) = ? LIMIT 1",
            (clean,),
        )
        row = await cursor.fetchone()
    return {"id": int(row["id"]), "email": str(row["email"])} if row else None


async def friend_suggestions(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """Друзья пользователя — подсказка для формы выдачи. Пусто, если слоя нет.

    Социальный модуль НЕ импортируется намеренно (миграция 229 приезжает
    отдельным срезом и может отсутствовать): сырой запрос, а любая ошибка =
    «подсказок нет». Страница при этом продолжает работать.
    """
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT u.id, u.email
                  FROM friendship AS f
                  JOIN users AS u ON u.id = f.friend_id
                 WHERE f.user_id = ?
                 ORDER BY u.email
                 LIMIT ?
                """,
                (int(user_id), int(limit)),
            )
            return [
                {"id": int(r["id"]), "email": str(r["email"])}
                for r in await cursor.fetchall()
            ]
    except Exception as exc:  # noqa: BLE001 — 229 может быть ещё не приземлена
        log.debug("llm_grant.friend_suggestions_unavailable", error=str(exc))
        return []


async def grantor_provider_name(grantor_id: int) -> str:
    """Имя провайдера выдающего — БЕЗ ключа и без каких-либо его настроек.

    Владелец держит выбор в глобальном ``kv_settings``, участник — в своём
    ``user_settings``; берём ровно эту одну строку.
    """
    from app.auth.owner import is_owner  # noqa: PLC0415
    from app.storage.repository import get_kv, get_user_kv  # noqa: PLC0415

    try:
        owner = await is_owner(int(grantor_id))
    except Exception:  # noqa: BLE001
        owner = False
    try:
        async with get_connection() as conn:
            if owner:
                raw = await get_kv(conn, "llm_provider") or await get_kv(
                    conn, "byo_api_provider"
                )
            else:
                raw = await get_user_kv(conn, int(grantor_id), "llm_provider")
    except Exception:  # noqa: BLE001
        return "?"
    return (raw or "").strip().lower() or "не выбран"


# ---------------------------------------------------------------------------
# Квота
# ---------------------------------------------------------------------------


async def consume_quota(grant_id: int, daily_limit: int, day: str | None = None) -> bool:
    """Списать один запрос. ``False`` — дневной лимит уже исчерпан.

    Весь смысл — в ОДНОМ стейтменте: ``INSERT ... ON CONFLICT DO UPDATE ...
    WHERE used < ?``. Если условие не выполнено, SQLite молча ничего не пишет
    (``rowcount == 0``) — то есть «проверить и увеличить» происходит без окна,
    в которое могли бы протиснуться два параллельных запроса и вместе выбить
    N+1-й вызов. Транзакция ``BEGIN IMMEDIATE`` (write_transaction) держится
    ровно на время этой записи: никаких сетевых вызовов внутри.
    """
    target = day or _today()
    limit = int(daily_limit)
    if limit <= 0:
        return False
    async with write_transaction() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO llm_grant_usage (grant_id, day, used)
            VALUES (?, ?, 1)
            ON CONFLICT(grant_id, day) DO UPDATE SET used = used + 1
             WHERE llm_grant_usage.used < ?
            """,
            (int(grant_id), target, limit),
        )
        granted = cursor.rowcount > 0
    if not granted:
        log.info("llm_grant.quota_exhausted", grant_id=int(grant_id), day=target)
    return granted


async def refund_quota(grant_id: int, day: str | None = None) -> None:
    """Вернуть списанный запрос (клиента собрать не удалось — платить не за что).

    Без этого неудачная сборка клиента съедала бы единицу лимита друга.
    """
    target = day or _today()
    try:
        async with write_transaction() as conn:
            await conn.execute(
                "UPDATE llm_grant_usage SET used = used - 1 "
                "WHERE grant_id = ? AND day = ? AND used > 0",
                (int(grant_id), target),
            )
    except Exception as exc:  # noqa: BLE001 — возврат best-effort
        log.debug("llm_grant.refund_failed", grant_id=int(grant_id), error=str(exc))


# ---------------------------------------------------------------------------
# Запись (страница /settings/llm/sharing)
# ---------------------------------------------------------------------------


async def upsert_grant(
    grantor_id: int,
    grantee_id: int,
    daily_limit: int,
    note: str | None = None,
) -> int:
    """Создать или обновить выдачу. Возвращает ``llm_grant.id``.

    Повторная выдача той же паре ОЖИВЛЯЕТ строку (``revoked_at = NULL``,
    ``enabled = 1``) — так «отозвал, потом передумал» работает, а UNIQUE-пара
    не размножается.
    """
    limit = max(1, min(int(daily_limit), MAX_DAILY_LIMIT))
    clean_note = (note or "").strip()[:200] or None
    async with write_transaction() as conn:
        await conn.execute(
            """
            INSERT INTO llm_grant (grantor_id, grantee_id, daily_limit, enabled, note)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(grantor_id, grantee_id) DO UPDATE SET
                daily_limit = excluded.daily_limit,
                enabled     = 1,
                note        = excluded.note,
                revoked_at  = NULL
            """,
            (int(grantor_id), int(grantee_id), limit, clean_note),
        )
        cursor = await conn.execute(
            "SELECT id FROM llm_grant WHERE grantor_id = ? AND grantee_id = ?",
            (int(grantor_id), int(grantee_id)),
        )
        row = await cursor.fetchone()
    return int(row["id"])


async def set_enabled(grantor_id: int, grant_id: int, enabled: bool) -> bool:
    """Пауза/возобновление. Обновляет строку ТОЛЬКО если она принадлежит ``grantor_id``."""
    async with write_transaction() as conn:
        cursor = await conn.execute(
            "UPDATE llm_grant SET enabled = ? WHERE id = ? AND grantor_id = ?",
            (1 if enabled else 0, int(grant_id), int(grantor_id)),
        )
        return cursor.rowcount > 0


async def set_limit(grantor_id: int, grant_id: int, daily_limit: int) -> bool:
    """Поменять дневной лимит (только своей выдаче)."""
    limit = max(1, min(int(daily_limit), MAX_DAILY_LIMIT))
    async with write_transaction() as conn:
        cursor = await conn.execute(
            "UPDATE llm_grant SET daily_limit = ? WHERE id = ? AND grantor_id = ?",
            (limit, int(grant_id), int(grantor_id)),
        )
        return cursor.rowcount > 0


async def revoke(grantor_id: int, grant_id: int) -> bool:
    """Отозвать насовсем: ``revoked_at`` + ``enabled = 0``."""
    async with write_transaction() as conn:
        cursor = await conn.execute(
            "UPDATE llm_grant SET revoked_at = datetime('now'), enabled = 0 "
            "WHERE id = ? AND grantor_id = ? AND revoked_at IS NULL",
            (int(grant_id), int(grantor_id)),
        )
        return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Статус для UI чата
# ---------------------------------------------------------------------------


async def borrowed_status(user_id: int) -> dict[str, Any] | None:
    """«Работаешь на модели друга» — данные для бейджа/баннера, БЕЗ ключа.

    ``None`` — человек либо на своей модели, либо вообще без модели. Квота при
    этом не тратится: функция только читает.
    """
    grants = await active_grants_for(int(user_id))
    if not grants:
        return None
    day = _today()
    for grant in grants:
        used = await usage_today(int(grant["id"]), day)
        limit = int(grant["daily_limit"])
        if used < limit:
            return {
                "grant_id": int(grant["id"]),
                "grantor_email": str(grant.get("grantor_email") or ""),
                "provider": await grantor_provider_name(int(grant["grantor_id"])),
                "daily_limit": limit,
                "used_today": used,
                "remaining": limit - used,
                "exhausted": False,
            }
    first = grants[0]
    limit = int(first["daily_limit"])
    return {
        "grant_id": int(first["id"]),
        "grantor_email": str(first.get("grantor_email") or ""),
        "provider": await grantor_provider_name(int(first["grantor_id"])),
        "daily_limit": limit,
        "used_today": await usage_today(int(first["id"]), day),
        "remaining": 0,
        "exhausted": True,
    }
