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

То же правило — про людей: НИ ОДНА функция здесь не отдаёт наружу сырой
e-mail. Человек называется ``name`` / ``*_name`` — отображаемым именем либо
МАСКОЙ адреса (см. :func:`_person_label`), ровно так же, как в поиске людей,
списке друзей и шапке переписки. Раньше адреса отдавались целиком, а прятал
их вызывающий: ``/settings/llm/sharing`` печатал почту всех твоих друзей в
``<datalist>``, а ``borrowed_status`` клала почту выдавшего в контекст шаблона
чата. Форма выдачи поэтому оперирует ``friend_id`` (:func:`friend_for_grant`),
а не адресом: в этом потоке почты нет вообще — ни в разметке, ни в POST-теле.

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

Сама ВЫДАЧА при этом теперь требует дружбы уже на входе: цель выбирается из
списка друзей (:func:`friend_for_grant`), а не по адресу. Раньше можно было
выдать доступ кому угодно по e-mail — но такая выдача всё равно не работала до
подтверждённой дружбы (``_friends_ok`` в резолвере), так что мы убрали не
возможность, а бесполезный промежуточный шаг вместе с утечкой адресов.
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


def _person_label(display_name: Any, email: Any) -> str:
    """Как назвать человека наружу: имя, иначе МАСКА адреса. Никогда не адрес.

    Почему это здесь, а не в шаблоне
    --------------------------------
    Раньше запросы ниже отдавали ``u.email`` целиком, а маскировал его роут
    ``/settings/llm/sharing``. То есть безопасность зависела от того, что
    каждый следующий потребитель вспомнит про маску, — и первый же, кто
    забудет, снова печатает чужую почту (именно так она и утекала: достаточно
    было подружиться, чтобы забрать настоящий адрес человека, включая адрес
    владельца инстанса). Теперь сырой адрес просто НЕ ВЫХОДИТ из этого модуля:
    маскировать нечего, потому что нечему утекать.

    Правило маскирования одно на весь продукт (поиск людей, список друзей,
    шапка переписки) и живёт в ``app.social.repository``. Импорт ленивый и
    защищённый — модуль намеренно не зависит от социального слоя (см.
    docstring файла); провал импорта отдаёт «аноним», но НИКОГДА не адрес.
    """
    clean = str(display_name or "").strip()
    if clean:
        return clean
    try:
        from app.social.repository import _mask_email  # noqa: PLC0415

        return _mask_email(str(email or ""))
    except Exception as exc:  # нет соц-слоя → безымянный, но НИКОГДА не адрес
        log.debug("llm_grant.mask_unavailable", error=str(exc))
        return "аноним"


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


# Адреса тут нет намеренно: резолверу LLM нужен ``grantor_id`` (по нему он
# читает провайдера и ключ выдавшего), а e-mail он клал в поле, которое никто
# не читает. JOIN на ``users`` был нужен ровно ради этого столбца — и ушёл
# вместе с ним; осиротевших выдач не бывает (FK grantor_id → ON DELETE CASCADE).
_ACTIVE_SQL = """
SELECT g.id, g.grantor_id, g.grantee_id, g.daily_limit, g.note, g.created_at
  FROM llm_grant AS g
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
    """«Я делюсь» — все НЕ отозванные выдачи пользователя + расход за сегодня.

    Вторая сторона называется ``grantee_name`` (имя или маска адреса, см.
    :func:`_person_label`). Сырого ``grantee_email`` в результате нет.
    """
    day = _today()
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT g.id, g.grantee_id, g.daily_limit, g.enabled, g.note,
                   g.created_at, u.email AS _email, u.display_name AS _display_name,
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
            row["grantee_name"] = _person_label(
                row.pop("_display_name", None), row.pop("_email", None)
            )
            row["friends"] = await _friends_ok(
                conn, int(grantor_id), int(row["grantee_id"])
            )
    return rows


async def list_received_by(grantee_id: int) -> list[dict[str, Any]]:
    """«Мне дали доступ» — выдачи в мою сторону + расход и провайдер выдавшего.

    Провайдер отдаётся ТОЛЬКО как имя (``openrouter``, ``ollama``…): ключ не
    читается вовсе, чтобы физически не было пути, по которому он утечёт в
    шаблон или в JSON. Выдавший — ``grantor_name`` (имя или маска), не адрес.
    """
    day = _today()
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT g.id, g.grantor_id, g.daily_limit, g.enabled, g.note,
                   g.created_at, u.email AS _email, u.display_name AS _display_name,
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
            row["grantor_name"] = _person_label(
                row.pop("_display_name", None), row.pop("_email", None)
            )
            row["friends"] = await _friends_ok(
                conn, int(row["grantor_id"]), int(grantee_id)
            )
            row["provider"] = await grantor_provider_name(int(row["grantor_id"]))
    return rows


_FRIENDS_SQL = """
SELECT u.id AS id, u.email AS email, u.display_name AS display_name
  FROM friendship AS f
  JOIN users AS u ON u.id = f.friend_id
 WHERE f.user_id = ?
"""


async def friend_suggestions(user_id: int, limit: int = 50) -> list[dict[str, Any]]:
    """Друзья пользователя — выбор для формы выдачи: ``{"id", "name"}``.

    Адреса тут нет: форма выдачи оперирует ``id`` друга, а человеку показывается
    имя (или маска). Раньше функция отдавала сырые e-mail'ы, они уезжали в
    ``<datalist>``, и «подружиться» превращалось в способ узнать чужую почту.

    Социальный модуль НЕ импортируется намеренно (миграция 229 приезжает
    отдельным срезом и может отсутствовать): сырой запрос, а любая ошибка =
    «выбирать не из кого». Страница при этом продолжает работать.
    """
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                _FRIENDS_SQL + " ORDER BY lower(COALESCE(u.display_name, u.email)) LIMIT ?",
                (int(user_id), max(1, min(int(limit), 500))),
            )
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 — 229 может быть ещё не приземлена
        log.debug("llm_grant.friend_suggestions_unavailable", error=str(exc))
        return []
    return [
        {"id": int(r["id"]), "name": _person_label(r["display_name"], r["email"])}
        for r in rows
    ]


async def friend_for_grant(user_id: int, friend_id: Any) -> dict[str, Any] | None:
    """Резолв цели выдачи по ``friend_id`` из формы. ``None`` — нельзя.

    Вход — id, пришедший от клиента, поэтому он ОБЯЗАН быть перепроверен по
    друзьям вызывающего, а не просто приведён к int: иначе форма выдачи
    превращается в «выдать доступ любому аккаунту по перебору номеров», а заодно
    в оракул «существует ли пользователь N».

    Возвращает ``{"id", "name"}`` — то же, что показывает список выбора, чтобы
    подтверждение называло человека ровно так же, как называла форма.
    """
    try:
        target = int(str(friend_id).strip())
    except (TypeError, ValueError):
        return None
    if target <= 0 or target == int(user_id):
        return None
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                _FRIENDS_SQL + " AND u.id = ? LIMIT 1", (int(user_id), target)
            )
            row = await cursor.fetchone()
    except Exception as exc:  # нет таблицы дружбы → выдавать некому
        log.debug("llm_grant.friend_lookup_unavailable", error=str(exc))
        return None
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "name": _person_label(row["display_name"], row["email"]),
    }


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


async def person_name(user_id: int) -> str:
    """Как назвать этого человека наружу: имя либо маска. Никогда не адрес."""
    try:
        async with get_connection() as conn:
            cursor = await conn.execute(
                "SELECT email, display_name FROM users WHERE id = ?", (int(user_id),)
            )
            row = await cursor.fetchone()
    except Exception as exc:  # БД занята → безымянный, но не адрес
        log.debug("llm_grant.person_name_failed", error=str(exc))
        return "аноним"
    if row is None:
        return "аноним"
    return _person_label(row["display_name"], row["email"])


async def borrowed_status(user_id: int) -> dict[str, Any] | None:
    """«Работаешь на модели друга» — данные для бейджа/баннера, БЕЗ ключа.

    ``None`` — человек либо на своей модели, либо вообще без модели. Квота при
    этом не тратится: функция только читает.

    Выдавший назван ``grantor_name`` (имя или маска). Раньше здесь лежал его
    сырой ``grantor_email``: этот словарь уезжает в контекст шаблона чата
    (``_provider_badge`` → ``badge["borrowed"]``), то есть чужой адрес был в
    одной правке разметки от того, чтобы быть напечатанным в шапке у того, кому
    модель одолжили.
    """
    grants = await active_grants_for(int(user_id))
    if not grants:
        return None
    day = _today()

    async def _card(grant: dict[str, Any], used: int, *, exhausted: bool) -> dict[str, Any]:
        limit = int(grant["daily_limit"])
        return {
            "grant_id": int(grant["id"]),
            "grantor_name": await person_name(int(grant["grantor_id"])),
            "provider": await grantor_provider_name(int(grant["grantor_id"])),
            "daily_limit": limit,
            "used_today": used,
            "remaining": 0 if exhausted else limit - used,
            "exhausted": exhausted,
        }

    for grant in grants:
        used = await usage_today(int(grant["id"]), day)
        if used < int(grant["daily_limit"]):
            return await _card(grant, used, exhausted=False)
    first = grants[0]
    return await _card(first, await usage_today(int(first["id"]), day), exhausted=True)
