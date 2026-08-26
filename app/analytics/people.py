"""Люди и рост: КТО зарегистрировался и КАК идёт прирост. Без содержимого.

Зачем отдельный модуль рядом с ``report.py``
-------------------------------------------
``report.py`` отвечает на вопрос «что происходит на сайте» — страницы, клики,
источники. Этот отвечает на другой: «кто пришёл и сколько их стало». Числа тут
считаются по ``users.created_at``, то есть работают за ВСЮ историю инстанса, а
не с того дня, когда включили счётчик событий, — и поэтому их нельзя держать в
общем отчёте, у которого честная граница данных стоит на дате запуска
аналитики.

────────────────────────────────────────────────────────────────────────────
ГРАНИЦА, РАДИ КОТОРОЙ ЭТОТ ФАЙЛ НАПИСАН ТАК, А НЕ ИНАЧЕ
────────────────────────────────────────────────────────────────────────────
Владелец попросил видеть ЛЮДЕЙ (адреса, даты, активность), а НЕ их переписку.
Разница между «страницей про людей» и «страницей, которая читает чужие чаты»
— это одна строчка SQL, добавленная через полгода «чтобы удобнее было». Чтобы
такую строчку нельзя было добавить незаметно, правило зафиксировано структурой,
а не комментарием:

1. **Разрешённые источники и колонки — исчерпывающий список.**

   ===================================  =======================================
   таблица                              что отсюда МОЖНО брать
   ===================================  =======================================
   ``users``                            id, email, role, status, created_at,
                                        last_login_at
   ``user_settings``                    ТОЛЬКО факт существования строки с
                                        ключом провайдера/BYO-ключа (значение
                                        не читается никогда — это чужой ключ)
   ``chat_session``                     ТОЛЬКО ``COUNT(*)`` и
                                        ``MAX(updated_at)``. Колонка ``title``
                                        генерируется из первого сообщения
                                        человека — это его текст, и её тут нет
   ``analytics_user_day`` /             ТОЛЬКО ``day`` (дата активности)
   ``analytics_event``
   ===================================  =======================================

2. **Названия таблиц с чужим текстом в этом файле не встречаются вообще.**
   Ни ``chat_message``, ни ``dm_message``, ни ``user_memory``, ни ``entity``,
   ни ``note``. Счётчики, для которых такие таблицы всё-таки нужны («написал
   первое сообщение»), берутся ГОТОВЫМИ из :mod:`app.analytics.funnel`, где они
   давно посчитаны через ``COUNT(DISTINCT user_id)``. Это проверяется тестом
   ``test_owner_people_view.py`` по ИСХОДНИКУ модуля: он ищет запрещённые имена
   как подстроки. Поэтому «просто дописать один SELECT» здесь не получится —
   тест покраснеет на самом имени таблицы.

3. **Наружу уходит :class:`Person` со ``slots=True``, а не строка БД.** У него
   ровно одиннадцать полей, каждое — идентификатор, дата, перечисление, счётчик
   или адрес почты. Свободного текста в нём нет НИ ОДНОГО поля, поэтому шаблону
   физически нечего отрисовать, кроме метаданных: чужой текст не «фильтруется»
   на выходе, ему просто некуда попасть. Список полей тоже под тестом.

   Отсюда же отсутствие ``display_name``: это единственное поле профиля,
   которое участник заполняет сам произвольным текстом. Ценность его на этой
   странице — «удобнее узнать человека», цена — открытый канал чужого текста в
   owner-страницу и размытая формулировка правила («текст нельзя, кроме вот
   этого»). Владелец просил адреса; адрес идентифицирует человека однозначно.

4. **E-mail показывается владельцу и НИКОМУ больше.** Он не уходит ни в
   ``log.*`` (в журналы этого модуля попадают только id и счётчики), ни в
   события аналитики (там колонки под адрес нет в принципе), ни в один ответ
   не-владельцу — роут ``/root/people`` резолвит владельца fail-closed до того,
   как этот модуль вообще будет вызван.

────────────────────────────────────────────────────────────────────────────
СРАВНЕНИЯ («сегодня против вчера»)
────────────────────────────────────────────────────────────────────────────
Неделя и месяц сравниваются ВЫРАВНЕННЫМИ отрезками: неполная текущая неделя —
против такого же по длине куска прошлой, а не против её полных семи дней.
Иначе в понедельник утром отчёт всегда показывал бы обвал на 85 %, и владелец
через две недели перестал бы на него смотреть. Что именно с чем сравнивается,
подписано в самих данных (``current_label`` / ``previous_label``), а не
подразумевается.

Прирост в процентах при НУЛЕВОЙ базе не определён: делить не на что. Такой
случай отдаёт ``pct=None`` («—»), а не 0 % и не 100 % — оба варианта были бы
выдуманным числом. Абсолютная дельта при этом честно показывается всегда.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.analytics.people")

#: Сколько месяцев показываем в помесячном графике.
MONTHS_SHOWN = 12

#: Окна дневного графика, которые просил владелец.
DAY_WINDOWS: tuple[int, ...] = (30, 90)


@dataclass(slots=True)
class Person:
    """Один аккаунт — МЕТАДАННЫЕ и только они.

    Каждое поле — id, адрес почты, дата, перечисление или счётчик. Свободного
    текста нет ни одного поля, и это не случайность: см. пункт 3 в шапке
    модуля. Новое поле здесь — это изменение границы приватности страницы, а
    не «ещё одна колонка в таблице».
    """

    id: int
    email: str
    role: str
    status: str
    created_at: str
    created_day: str
    last_login_at: str
    last_active: str
    chat_sessions: int
    llm_configured: bool
    is_owner: bool


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def pct_change(current: int, previous: int) -> float | None:
    """Прирост в процентах или ``None``, если базы нет.

    ``previous == 0`` — не «рост на 100 %» и не «рост на 0 %»: это отсутствие
    базы, и любое число тут было бы выдуманным. Шаблон рисует прочерк, а рядом
    всегда стоит абсолютная дельта, которая читается и без базы.
    """
    if previous <= 0:
        return None
    return round((current - previous) * 100.0 / previous, 1)


# ── Люди ─────────────────────────────────────────────────────────────────────


async def _accounts() -> list[dict[str, Any]]:
    """Аккаунты. Колонки — ровно те, что разрешены таблицей в шапке модуля."""
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id, email, role, status, created_at, last_login_at "
            "FROM users ORDER BY datetime(created_at) DESC, id DESC"
        )
        rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def _chat_counts() -> dict[int, tuple[int, str]]:
    """``user_id -> (сколько чатов, дата последнего)``. Только COUNT и MAX.

    Заголовки чатов не запрашиваются: они собираются из первой реплики
    человека, то есть являются его текстом.
    """
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT user_id, COUNT(*) AS n, MAX(substr(updated_at, 1, 10)) AS last "
                "FROM chat_session GROUP BY user_id"
            )
            rows = await cur.fetchall()
    except Exception as exc:  # noqa: BLE001 — список людей важнее счётчика чатов
        log.debug("people.chat_counts_failed", error=str(exc))
        return {}
    return {int(r["user_id"]): (int(r["n"] or 0), str(r["last"] or "")) for r in rows}


async def _last_seen() -> dict[int, str]:
    """``user_id -> последний день активности`` по данным счётчика событий."""
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT user_id, MAX(day) AS last FROM ("
                "  SELECT user_id, day FROM analytics_user_day"
                "  UNION ALL"
                "  SELECT user_id, day FROM analytics_event WHERE user_id IS NOT NULL"
                ") GROUP BY user_id"
            )
            rows = await cur.fetchall()
    except Exception as exc:  # noqa: BLE001 — на БД без миграции 234 таблиц нет
        log.debug("people.last_seen_failed", error=str(exc))
        return {}
    return {int(r["user_id"]): str(r["last"] or "") for r in rows if r["user_id"]}


async def list_people(*, owner_id: int | None = None) -> list[Person]:
    """Все аккаунты как :class:`Person`. Новее — выше."""
    accounts = await _accounts()
    uids = [int(a["id"]) for a in accounts]
    chats = await _chat_counts()
    seen = await _last_seen()

    # Признак «подключил модель» НЕ переписывается здесь заново: та же функция
    # обслуживает ступень воронки, и два независимых определения «подключил»
    # разъехались бы на первой же правке.
    from app.analytics import funnel  # noqa: PLC0415 — circular-safe, ленивый

    try:
        configured = await funnel.configured_llm_ids(uids, owner_id)
    except Exception as exc:  # noqa: BLE001
        log.debug("people.llm_ids_failed", error=str(exc))
        configured = set()

    out: list[Person] = []
    for a in accounts:
        uid = int(a["id"])
        n_chats, chat_day = chats.get(uid, (0, ""))
        created = str(a["created_at"] or "")
        login = str(a["last_login_at"] or "")
        out.append(
            Person(
                id=uid,
                email=str(a["email"] or ""),
                role=str(a["role"] or "—"),
                status=str(a["status"] or "—"),
                created_at=created,
                created_day=created[:10],
                last_login_at=login,
                # «Последний раз видели» — максимум из трёх независимых следов.
                # Ни один из них по отдельности не полон: счётчик событий мог
                # быть выключен, чат мог не открываться, вход мог быть давно.
                last_active=max(seen.get(uid, ""), chat_day, login[:10]),
                chat_sessions=n_chats,
                llm_configured=uid in configured,
                is_owner=str(a["role"] or "") == "owner" or uid == owner_id,
            )
        )
    return out


# ── Рост ─────────────────────────────────────────────────────────────────────


async def signups_by_month(months: int = MONTHS_SHOWN) -> list[dict[str, Any]]:
    """Регистрации по календарным месяцам (последние ``months``), старые слева.

    Месяц берётся как ``substr(created_at, 1, 7)`` — той же нарезкой, что и
    сутки в ``store.registrations_by_day``, чтобы дневной и месячный график
    не расходились на границе часового пояса: обе отметки времени в этой базе
    пишутся в UTC.
    """
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT substr(created_at, 1, 7) AS month, COUNT(*) AS n FROM users "
            "GROUP BY month ORDER BY month DESC LIMIT ?",
            (max(1, months),),
        )
        rows = await cur.fetchall()
    return [{"month": str(r["month"]), "count": int(r["n"])} for r in reversed(rows)]


async def total_accounts() -> int:
    async with get_connection() as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM users")
        row = await cur.fetchone()
    return int(row[0] or 0)


def _month_start(day: date) -> date:
    return day.replace(day=1)


def compare_windows(today: date) -> list[dict[str, Any]]:
    """Три сравнения, которые просил владелец, как ОТРЕЗКИ ДАТ.

    Отдельная чистая функция, потому что вся арифметика границ (в том числе
    «а что если сегодня первое число») проверяется тестом без базы.
    """
    yesterday = today - timedelta(days=1)

    week_start = today - timedelta(days=today.weekday())
    span_days = (today - week_start).days  # 0 в понедельник, 6 в воскресенье
    prev_week_start = week_start - timedelta(days=7)
    prev_week_end = prev_week_start + timedelta(days=span_days)

    month_start = _month_start(today)
    prev_month_end = month_start - timedelta(days=1)
    prev_month_start = _month_start(prev_month_end)
    # Выравниваем по числу месяца, но не вылезаем за его конец: 31 марта
    # сравнивается с 28/29 февраля, а не с несуществующим 31 февраля.
    prev_month_aligned = min(
        prev_month_start + timedelta(days=today.day - 1), prev_month_end
    )

    return [
        {
            "key": "day",
            "title": "Сегодня",
            "current": (today, today),
            "previous": (yesterday, yesterday),
            "current_label": today.isoformat(),
            "previous_label": f"вчера, {yesterday.isoformat()}",
        },
        {
            "key": "week",
            "title": "Эта неделя",
            "current": (week_start, today),
            "previous": (prev_week_start, prev_week_end),
            "current_label": f"{week_start.isoformat()} — {today.isoformat()}",
            "previous_label": (
                f"{prev_week_start.isoformat()} — {prev_week_end.isoformat()}"
            ),
        },
        {
            "key": "month",
            "title": "Этот месяц",
            "current": (month_start, today),
            "previous": (prev_month_start, prev_month_aligned),
            "current_label": f"{month_start.isoformat()} — {today.isoformat()}",
            "previous_label": (
                f"{prev_month_start.isoformat()} — {prev_month_aligned.isoformat()}"
            ),
        },
    ]


async def growth(*, today: str | None = None) -> dict[str, Any]:
    """Графики прироста и три сравнения с прошлым периодом."""
    from app.analytics import report, store  # noqa: PLC0415 — тяжёлые модули

    ref = date.fromisoformat(today or _today())
    windows = compare_windows(ref)

    # Одна выборка на всё: самый ранний день среди сравнений и графиков.
    earliest = min(
        [w["previous"][0] for w in windows]
        + [ref - timedelta(days=max(DAY_WINDOWS) - 1)]
    )
    by_day = await store.registrations_by_day(earliest.isoformat(), ref.isoformat())

    def total(span: tuple[date, date]) -> int:
        start, end = span
        return sum(
            by_day.get(d, 0) for d in store.iter_days(start.isoformat(), end.isoformat())
        )

    compare: list[dict[str, Any]] = []
    for w in windows:
        cur = total(w["current"])
        prev = total(w["previous"])
        compare.append(
            {
                "key": w["key"],
                "title": w["title"],
                "current": cur,
                "previous": prev,
                "delta": cur - prev,
                "pct": pct_change(cur, prev),
                "current_label": w["current_label"],
                "previous_label": w["previous_label"],
            }
        )

    charts: dict[str, Any] = {}
    for window in DAY_WINDOWS:
        start = ref - timedelta(days=window - 1)
        days = list(store.iter_days(start.isoformat(), ref.isoformat()))
        series = [by_day.get(d, 0) for d in days]
        charts[str(window)] = {
            "days": days,
            "series": series,
            "bars": report.bar_geometry(series, width=680, height=90),
            "total": sum(series),
            "peak": max(series) if series else 0,
        }

    months = await signups_by_month(MONTHS_SHOWN)
    month_values = [m["count"] for m in months]
    return {
        "today": ref.isoformat(),
        "total": await total_accounts(),
        "compare": compare,
        "charts": charts,
        "months": months,
        "month_bars": report.bar_geometry(month_values, width=680, height=90),
        "month_peak": max(month_values) if month_values else 0,
    }


# ── Удержание ЛЮДЕЙ (не страниц) ─────────────────────────────────────────────


async def _cohort_ids(day_from: str, day_to: str) -> list[int]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM users "
            "WHERE substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ? "
            "ORDER BY id",
            (day_from, day_to),
        )
        return [int(r[0]) for r in await cur.fetchall()]


async def _returned_since(uids: list[int], day: str) -> int:
    """Сколько человек из когорты появлялись НА САЙТЕ начиная с дня ``day``."""
    if not uids:
        return 0
    marks = ", ".join("?" for _ in uids)
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT COUNT(DISTINCT user_id) FROM ("  # noqa: S608 — только "?, ?"
                f"  SELECT user_id, day FROM analytics_user_day WHERE user_id IN ({marks})"
                "  UNION ALL"
                f"  SELECT user_id, day FROM analytics_event WHERE user_id IN ({marks})"
                ") WHERE day >= ?",
                (*uids, *uids, day),
            )
            row = await cur.fetchone()
    except Exception as exc:  # noqa: BLE001 — БД без миграции 234
        log.debug("people.returned_failed", error=str(exc))
        return 0
    return int(row[0] or 0)


async def cohort_health(
    *, today: str | None = None, owner_id: int | None = None
) -> dict[str, Any]:
    """Что стало с теми, кто зарегистрировался НА ПРОШЛОЙ НЕДЕЛЕ.

    Три числа — вернулись, подключили модель, написали первое сообщение.
    Последние два НЕ считаются здесь заново: они берутся ступенями уже
    существующей воронки (:func:`app.analytics.funnel.build_funnel`) по той же
    когорте. Вторая реализация тех же ступеней означала бы две цифры «подключили
    модель» на соседних страницах, расходящиеся после первой правки.
    """
    from app.analytics import funnel, store  # noqa: PLC0415

    ref = date.fromisoformat(today or _today())
    this_week_start = ref - timedelta(days=ref.weekday())
    last_week_start = this_week_start - timedelta(days=7)
    last_week_end = this_week_start - timedelta(days=1)

    uids = await _cohort_ids(last_week_start.isoformat(), last_week_end.isoformat())
    data = await funnel.build_funnel(
        last_week_start.isoformat(), last_week_end.isoformat(), owner_id=owner_id
    )
    stages = {s["key"]: int(s["count"]) for s in data["stages"]}
    cohort = len(uids)
    returned = await _returned_since(uids, this_week_start.isoformat())
    # Возврат измерим только с того дня, когда счётчик событий начал писать.
    # У когорты старше него «0 вернулось» — это не факт, а отсутствие данных.
    coverage = await store.coverage()
    measurable = bool(
        coverage["first_day"] and coverage["first_day"] <= this_week_start.isoformat()
    )

    def share(n: int) -> float | None:
        return round(n * 100.0 / cohort, 1) if cohort else None

    return {
        "week_from": last_week_start.isoformat(),
        "week_to": last_week_end.isoformat(),
        "since": this_week_start.isoformat(),
        "cohort": cohort,
        "measurable": measurable,
        "rows": [
            {
                "title": "Вернулись на этой неделе",
                "count": returned,
                "pct": share(returned),
                "measurable": measurable,
                "source": "analytics_user_day",
            },
            {
                "title": "Подключили модель",
                "count": stages.get("llm", 0),
                "pct": share(stages.get("llm", 0)),
                "measurable": True,
                "source": "воронка: user_settings / kv llm_provider",
            },
            {
                "title": "Написали первое сообщение",
                "count": stages.get("first_message", 0),
                "pct": share(stages.get("first_message", 0)),
                "measurable": True,
                "source": "воронка: COUNT(DISTINCT user_id)",
            },
        ],
    }


async def build_people_view(
    *, owner_id: int | None = None, today: str | None = None
) -> dict[str, Any]:
    """Всё, что показывает ``/root/people``, одним словарём."""
    ref = today or _today()
    people = await list_people(owner_id=owner_id)
    return {
        "today": ref,
        "people": people,
        "counts": {
            "total": len(people),
            "active": sum(1 for p in people if p.status == "active"),
            "suspended": sum(1 for p in people if p.status == "suspended"),
            "pending": sum(1 for p in people if p.status == "pending"),
            "with_llm": sum(1 for p in people if p.llm_configured),
        },
        "growth": await growth(today=ref),
        "cohort": await cohort_health(today=ref, owner_id=owner_id),
    }


__all__ = [
    "DAY_WINDOWS",
    "MONTHS_SHOWN",
    "Person",
    "build_people_view",
    "cohort_health",
    "compare_windows",
    "growth",
    "list_people",
    "pct_change",
    "signups_by_month",
    "total_accounts",
]
