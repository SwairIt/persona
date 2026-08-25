"""Весь SQL первосторонней аналитики. Единственный модуль, который знает схему.

Почему отдельный модуль, а не запросы в роуте
--------------------------------------------
``tests/test_architecture_gates.py`` запрещает новым файлам в
``app/web/routes/`` импортировать ``get_connection``/``write_transaction``.
Это не бюрократия: страница аналитики — самый соблазнительный кандидат на
«ну я тут по-быстрому просканирую табличку», а сканирование сырых событий на
каждом открытии дашборда — ровно тот запрос, который кладёт этот сервер.
Держим SQL здесь, чтобы стоимость каждого чтения была видна в одном файле.

Как читается отчёт (и почему он НЕ сканирует сырьё)
---------------------------------------------------
Сутки бывают двух видов:

* **закрытые** — свёрнуты в ``analytics_daily`` (плюс ``analytics_daily_unique``
  и ``analytics_user_day``). Граница хранится в kv ``analytics_rollup_through``
  = последний ПОЛНОСТЬЮ свёрнутый день;
* **сегодняшние/несвёрнутые** — читаются из ``analytics_event`` напрямую.

Все агрегаты собираются одним ``UNION ALL`` над этими двумя источниками: у
``analytics_daily`` и ``analytics_event`` намеренно ОДИНАКОВЫЕ имена колонок
(``path``/``label``/``role``/``device``/``referrer_host``), поэтому свёртка
подставляется в тот же запрос как «событие с весом ``hits``». За 30 дней
отчёта сырых строк читается максимум за один сегодняшний день.

Свёртка и вычистка окна вызываются ЛЕНИВО — из обработчика дашборда, перед
чтением (:func:`app.analytics.report.build_dashboard`). Отдельный воркер сюда
не заводится намеренно: у инстанса их и так много, а аналитику смотрит один
человек и не чаще, чем раз в день.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection, write_transaction
from app.storage.repository import get_kv

log = get_logger("persona.analytics.store")

#: kv: последний ПОЛНОСТЬЮ свёрнутый день (``YYYY-MM-DD``). Пусто = ничего.
KV_ROLLUP_THROUGH = "analytics_rollup_through"
#: kv: сколько суток держим сырые события. По умолчанию 90.
KV_RETENTION_DAYS = "analytics_retention_days"
DEFAULT_RETENTION_DAYS = 90

#: Колонки, по которым разрешено группировать. Белый список, потому что имена
#: подставляются в SQL текстом (параметризовать идентификаторы нельзя).
_GROUPABLE: frozenset[str] = frozenset(
    {"path", "label", "role", "device", "referrer_host", "day", "kind"}
)

#: Поля строки события в том порядке, в каком их ждёт INSERT.
EVENT_COLUMNS: tuple[str, ...] = (
    "occurred_at",
    "day",
    "kind",
    "path",
    "label",
    "role",
    "device",
    "referrer_host",
    "session_hash",
    "user_id",
    "first_view",
    "status",
)

_INSERT_SQL = (
    # noqa ниже: имена колонок берутся из EVENT_COLUMNS (константа модуля),
    # значения — только через ?-плейсхолдеры. Пользовательского ввода в тексте
    # запроса нет.
    "INSERT INTO analytics_event ("  # noqa: S608
    + ", ".join(EVENT_COLUMNS)
    + ") VALUES ("
    + ", ".join("?" for _ in EVENT_COLUMNS)
    + ")"
)


def _row_tuple(event: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(event.get(col) for col in EVENT_COLUMNS)


async def _set_kv_tx(conn: Any, key: str, value: str) -> None:
    """Upsert в kv БЕЗ коммита — внутри уже открытой ``BEGIN IMMEDIATE``.

    ``app.storage.repository.set_kv`` в конце зовёт ``conn.commit()``, а это
    закрывает явную транзакцию, открытую :func:`write_transaction`, и её
    собственный ``COMMIT`` падает с «cannot commit - no transaction is active».
    Внутри транзакции нужен именно немой upsert.
    """
    await conn.execute(
        "INSERT INTO kv_settings (key, value, updated_at) "
        "VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "                               updated_at = datetime('now')",
        (key, value),
    )


# ── запись ────────────────────────────────────────────────────────────────────


async def insert_events(events: Sequence[dict[str, Any]]) -> int:
    """Записать пачку событий одной транзакцией. Возвращает число строк.

    Пачкой, а не по одному: каждая транзакция SQLite — это fsync, и сотня
    отдельных вставок на пике трафика стоит дороже, чем весь остальной запрос.
    Вызывается ТОЛЬКО из фонового флашера (:mod:`app.analytics.capture`), то
    есть никогда не находится на пути ответа пользователю.
    """
    if not events:
        return 0
    rows = [_row_tuple(e) for e in events]
    async with write_transaction() as conn:
        await conn.executemany(_INSERT_SQL, rows)
    return len(rows)


# ── свёртка ───────────────────────────────────────────────────────────────────


async def rollup_state() -> str | None:
    async with get_connection() as conn:
        return await get_kv(conn, KV_ROLLUP_THROUGH)


async def rollup_closed_days(today: str | None = None) -> list[str]:
    """Свернуть все ЗАКРЫТЫЕ сутки, которых ещё нет в ``analytics_daily``.

    «Закрытые» = строго раньше ``today``: сегодняшний день ещё дописывается, и
    свернуть его значило бы заморозить неполные цифры. Идемпотентно: перед
    вставкой день удаляется из свёрточных таблиц, поэтому повторный вызов
    (два воркера открыли дашборд одновременно) не удваивает счётчики.

    Возвращает список свёрнутых дней — им же меряется «а был ли смысл».
    """
    today = today or datetime.now(UTC).strftime("%Y-%m-%d")
    async with get_connection() as conn:
        through = await get_kv(conn, KV_ROLLUP_THROUGH)
        cur = await conn.execute(
            "SELECT DISTINCT day FROM analytics_event "
            "WHERE day < ? AND (? IS NULL OR day > ?) ORDER BY day",
            (today, through, through),
        )
        days = [str(r[0]) for r in await cur.fetchall()]
    if not days:
        return []
    async with write_transaction() as conn:
        for day in days:
            await conn.execute("DELETE FROM analytics_daily WHERE day = ?", (day,))
            await conn.execute(
                "DELETE FROM analytics_daily_unique WHERE day = ?", (day,)
            )
            await conn.execute("DELETE FROM analytics_user_day WHERE day = ?", (day,))
            await conn.execute(
                "INSERT INTO analytics_daily "
                "(day, kind, path, label, role, device, referrer_host, hits) "
                "SELECT day, kind, path, label, role, device, referrer_host, COUNT(*) "
                "FROM analytics_event WHERE day = ? "
                "GROUP BY day, kind, path, label, role, device, referrer_host",
                (day,),
            )
            await conn.execute(
                "INSERT INTO analytics_daily_unique (day, role, sessions, users) "
                "SELECT day, role, COUNT(DISTINCT session_hash), "
                "       COUNT(DISTINCT user_id) "
                "FROM analytics_event WHERE day = ? GROUP BY day, role",
                (day,),
            )
            await conn.execute(
                "INSERT OR IGNORE INTO analytics_user_day (day, user_id) "
                "SELECT DISTINCT day, user_id FROM analytics_event "
                "WHERE day = ? AND user_id IS NOT NULL",
                (day,),
            )
        await _set_kv_tx(conn, KV_ROLLUP_THROUGH, days[-1])
    log.info("analytics.rollup", days=len(days), through=days[-1])
    return days


async def purge_old_events(
    retention_days: int | None = None, *, today: str | None = None
) -> int:
    """Снести сырые события старше окна. Свёртка НЕ трогается.

    Окно — ``analytics_retention_days`` суток (kv, по умолчанию 90). Граница
    ВКЛЮЧАЮЩАЯ: день ``today - retention_days + 1`` остаётся, всё строго
    раньше — удаляется. То есть при окне 90 в базе всегда ровно 90 суток
    сырья, включая сегодняшние.

    Свёрнутые сутки переживают вычистку намеренно: в ``analytics_daily`` нет ни
    одного идентификатора, это чистые счётчики, и терять историю посещаемости
    ради приватности, которой там уже нет, — потеря без выигрыша.
    """
    if retention_days is None:
        retention_days = await retention_window()
    ref = date.fromisoformat(today or datetime.now(UTC).strftime("%Y-%m-%d"))
    cutoff = (ref - timedelta(days=max(1, retention_days) - 1)).isoformat()
    async with write_transaction() as conn:
        cur = await conn.execute(
            "DELETE FROM analytics_event WHERE day < ?", (cutoff,)
        )
        removed = int(cur.rowcount or 0)
    if removed:
        log.info("analytics.purged", rows=removed, cutoff=cutoff)
    return removed


async def save_settings(*, enabled: str, retention_days: int | None) -> None:
    """Записать рубильник и окно хранения (владелец, ``/root/analytics``).

    Живёт здесь, а не в роуте: ``tests/test_architecture_gates.py`` не пускает
    новый роут к ``write_transaction`` напрямую, и это правильно — весь SQL
    аналитики должен быть в одном файле, включая две строчки настроек.
    """
    async with write_transaction() as conn:
        await _set_kv_tx(conn, "analytics_enabled", "1" if enabled == "1" else "0")
        if retention_days is not None:
            await _set_kv_tx(conn, KV_RETENTION_DAYS, str(retention_days))


async def retention_window() -> int:
    async with get_connection() as conn:
        raw = await get_kv(conn, KV_RETENTION_DAYS)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    return value if value > 0 else DEFAULT_RETENTION_DAYS


# ── чтение ────────────────────────────────────────────────────────────────────


def _group_sql(group: Sequence[str]) -> str:
    bad = [c for c in group if c not in _GROUPABLE]
    if bad:
        raise ValueError(f"недопустимая колонка группировки: {bad}")
    return ", ".join(group)


async def aggregate(
    day_from: str,
    day_to: str,
    *,
    group: Sequence[str] = ("path",),
    kind: str | None = None,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """Счётчики за период поверх свёртки + несвёрнутого хвоста.

    Свёрнутые сутки дают строку с весом ``hits``, сырые — вес 1 на событие;
    ``UNION ALL`` + ``GROUP BY`` складывает их как один источник.
    """
    cols = _group_sql(group)
    kind_clause = " AND kind = ?" if kind else ""
    async with get_connection() as conn:
        through = await get_kv(conn, KV_ROLLUP_THROUGH) or ""
        params: list[Any] = [day_from, day_to, through]
        if kind:
            params.append(kind)
        params += [day_from, day_to, through]
        if kind:
            params.append(kind)
        # noqa ниже: ``cols`` собран из _GROUPABLE (белый список имён колонок,
        # см. _group_sql — чужое имя бросает ValueError), даты и kind идут
        # параметрами. Текст запроса не содержит пользовательского ввода.
        sql = (
            f"SELECT {cols}, SUM(hits) AS hits FROM ("  # noqa: S608
            f"  SELECT {cols}, hits FROM analytics_daily"
            f"   WHERE day >= ? AND day <= ? AND day <= ?{kind_clause}"
            "   UNION ALL "
            f"  SELECT {cols}, 1 AS hits FROM analytics_event"
            f"   WHERE day >= ? AND day <= ? AND day > ?{kind_clause}"
            f") GROUP BY {cols} ORDER BY hits DESC"
        )
        if limit > 0:
            sql += f" LIMIT {int(limit)}"
        cur = await conn.execute(sql, params)
        rows = await cur.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = {name: row[i] for i, name in enumerate(group)}
        item["hits"] = int(row[len(group)])
        out.append(item)
    return out


async def unique_sessions_by_day(day_from: str, day_to: str) -> dict[str, int]:
    """Уникальные псевдонимы сессий по суткам (свёртка + сырой хвост)."""
    async with get_connection() as conn:
        through = await get_kv(conn, KV_ROLLUP_THROUGH) or ""
        cur = await conn.execute(
            "SELECT day, SUM(sessions) FROM analytics_daily_unique "
            "WHERE day >= ? AND day <= ? AND day <= ? GROUP BY day",
            (day_from, day_to, through),
        )
        out = {str(r[0]): int(r[1] or 0) for r in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT day, COUNT(DISTINCT session_hash) FROM analytics_event "
            "WHERE day >= ? AND day <= ? AND day > ? AND session_hash IS NOT NULL "
            "GROUP BY day",
            (day_from, day_to, through),
        )
        for r in await cur.fetchall():
            out[str(r[0])] = out.get(str(r[0]), 0) + int(r[1] or 0)
    return out


async def active_users_by_day(day_from: str, day_to: str) -> dict[str, int]:
    """DAU по аккаунтам (свёрнутый ``analytics_user_day`` + сырой хвост)."""
    async with get_connection() as conn:
        through = await get_kv(conn, KV_ROLLUP_THROUGH) or ""
        cur = await conn.execute(
            "SELECT day, COUNT(DISTINCT user_id) FROM analytics_user_day "
            "WHERE day >= ? AND day <= ? AND day <= ? GROUP BY day",
            (day_from, day_to, through),
        )
        out = {str(r[0]): int(r[1] or 0) for r in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT day, COUNT(DISTINCT user_id) FROM analytics_event "
            "WHERE day >= ? AND day <= ? AND day > ? AND user_id IS NOT NULL "
            "GROUP BY day",
            (day_from, day_to, through),
        )
        for r in await cur.fetchall():
            out[str(r[0])] = out.get(str(r[0]), 0) + int(r[1] or 0)
    return out


async def weekly_active_users(day_from: str, day_to: str) -> int:
    """WAU: сколько РАЗНЫХ аккаунтов было активно за период (без двойного счёта)."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM ("
            "  SELECT user_id FROM analytics_user_day"
            "   WHERE day >= ? AND day <= ?"
            "  UNION "
            "  SELECT user_id FROM analytics_event"
            "   WHERE day >= ? AND day <= ? AND user_id IS NOT NULL"
            ")",
            (day_from, day_to, day_from, day_to),
        )
        row = await cur.fetchone()
    return int(row[0] or 0)


async def live_sessions(minutes: int = 15) -> dict[str, int]:
    """«Сейчас на сайте»: уникальные сессии и хиты за последние N минут.

    Читает только сырые события — а они за 15 минут заведомо помещаются в
    индекс по ``occurred_at``.
    """
    since = (datetime.now(UTC) - timedelta(minutes=minutes)).strftime(
        "%Y-%m-%dT%H:%M:%S"
    )
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(DISTINCT COALESCE(session_hash, 'anon-' || id)), COUNT(*) "
            "FROM analytics_event WHERE occurred_at >= ?",
            (since,),
        )
        row = await cur.fetchone()
    return {"sessions": int(row[0] or 0), "hits": int(row[1] or 0), "minutes": minutes}


async def coverage() -> dict[str, Any]:
    """Честная рамка отчёта: с какого дня вообще есть данные и сколько их.

    Дашборд обязан это показывать. Пустой график за 30 дней у продукта, где
    аналитика включилась вчера, читается как «трафика нет», а это враньё.
    """
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT MIN(day), MAX(day), COUNT(*) FROM analytics_event"
        )
        raw_min, raw_max, raw_count = await cur.fetchone()
        cur = await conn.execute("SELECT MIN(day), MAX(day) FROM analytics_daily")
        agg_min, agg_max = await cur.fetchone()
    first = min([d for d in (raw_min, agg_min) if d], default=None)
    last = max([d for d in (raw_max, agg_max) if d], default=None)
    return {
        "first_day": first,
        "last_day": last,
        "raw_events": int(raw_count or 0),
        "rolled_through": await rollup_state(),
    }


async def registrations_by_day(day_from: str, day_to: str) -> dict[str, int]:
    """Регистрации по суткам из ``users.created_at`` — источник ретроспективный.

    Событий аналитики для этого не нужно вовсе: таблица ``users`` уже хранит
    дату создания, поэтому график регистраций работает и за те месяцы, когда
    никакой аналитики на инстансе не было.
    """
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT substr(created_at, 1, 10) AS day, COUNT(*) FROM users "
            "WHERE substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ? "
            "GROUP BY day",
            (day_from, day_to),
        )
        return {str(r[0]): int(r[1]) for r in await cur.fetchall()}


async def cohort_retention(weeks: int = 4, *, today: str | None = None) -> list[dict]:
    """Удержание недельных когорт: зарегались на неделе N — вернулись ли потом.

    «Вернулся» = есть хотя бы один день активности СТРОГО ПОЗЖЕ дня
    регистрации. Считается по ``analytics_user_day`` + сырому хвосту, поэтому
    честная граница — день запуска аналитики: у когорт старше него возврат
    измерить нечем, и в отчёт уходит ``measurable=False``.
    """
    ref = date.fromisoformat(today or datetime.now(UTC).strftime("%Y-%m-%d"))
    cov = await coverage()
    since = cov["first_day"]
    out: list[dict] = []
    async with get_connection() as conn:
        for i in range(weeks):
            start = ref - timedelta(days=ref.weekday() + 7 * i)
            end = start + timedelta(days=6)
            cur = await conn.execute(
                "SELECT id, substr(created_at, 1, 10) FROM users "
                "WHERE substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ?",
                (start.isoformat(), end.isoformat()),
            )
            members = [(int(r[0]), str(r[1])) for r in await cur.fetchall()]
            returned = 0
            for uid, created in members:
                cur = await conn.execute(
                    "SELECT 1 FROM ("
                    "  SELECT day FROM analytics_user_day WHERE user_id = ?"
                    "  UNION SELECT day FROM analytics_event WHERE user_id = ?"
                    ") WHERE day > ? LIMIT 1",
                    (uid, uid, created),
                )
                if await cur.fetchone():
                    returned += 1
            out.append(
                {
                    "week_start": start.isoformat(),
                    "week_end": end.isoformat(),
                    "signups": len(members),
                    "returned": returned,
                    # Мерить возврат можно только с того дня, когда аналитика
                    # вообще начала писать. Иначе «0 вернулось» — не факт.
                    "measurable": bool(since and end.isoformat() >= since),
                }
            )
    return out


async def prune_user(user_id: int) -> int:
    """Стереть поведенческий след одного аккаунта (право на удаление).

    Обычно не вызывается: ``ON DELETE CASCADE`` в миграции 234 делает это
    автоматически при удалении строки ``users``. Функция нужна для случая
    «удалить след, но оставить аккаунт» — и чтобы поведение было выражено
    кодом, а не только внешним ключом.
    """
    async with write_transaction() as conn:
        cur = await conn.execute(
            "DELETE FROM analytics_event WHERE user_id = ?", (user_id,)
        )
        removed = int(cur.rowcount or 0)
        await conn.execute(
            "DELETE FROM analytics_user_day WHERE user_id = ?", (user_id,)
        )
    return removed


async def user_events(user_id: int, limit: int = 5000) -> list[dict[str, Any]]:
    """След одного аккаунта — для выгрузки «мои данные» (152-ФЗ, ст. 14).

    Отдаёт СЫРЫЕ строки этого пользователя без псевдонима сессии: сам
    ``session_hash`` — производная от его же токена, показывать её в файле,
    который ляжет в «Загрузки», незачем.
    """
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT occurred_at, kind, path, label, role, device, referrer_host "
            "FROM analytics_event WHERE user_id = ? ORDER BY id LIMIT ?",
            (user_id, limit),
        )
        rows = await cur.fetchall()
    keys = ("occurred_at", "kind", "path", "label", "role", "device", "referrer_host")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def iter_days(day_from: str, day_to: str) -> Iterable[str]:
    """Все сутки диапазона включительно — чтобы дырки в графике были нулями."""
    start = date.fromisoformat(day_from)
    end = date.fromisoformat(day_to)
    while start <= end:
        yield start.isoformat()
        start += timedelta(days=1)


__all__ = [
    "DEFAULT_RETENTION_DAYS",
    "EVENT_COLUMNS",
    "KV_RETENTION_DAYS",
    "KV_ROLLUP_THROUGH",
    "active_users_by_day",
    "aggregate",
    "cohort_retention",
    "coverage",
    "insert_events",
    "iter_days",
    "live_sessions",
    "prune_user",
    "purge_old_events",
    "registrations_by_day",
    "retention_window",
    "rollup_closed_days",
    "rollup_state",
    "save_settings",
    "unique_sessions_by_day",
    "user_events",
    "weekly_active_users",
]
