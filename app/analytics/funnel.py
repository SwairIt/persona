"""Воронка регистрации. Считается по УЖЕ СУЩЕСТВУЮЩИМ таблицам, где только можно.

Почему не «просто события»
--------------------------
Если бы каждая ступень воронки считалась по нашим же событиям, воронка начала
бы работать с того дня, когда включили аналитику, и на инстансе, у которого
уже есть зарегистрированные люди, показала бы честные нули по всем ступеням.
Это самый бесполезный вид отчёта: выглядит как данные, а данных нет.

Поэтому ступени разделены по ИСТОЧНИКУ:

======================  =======================================  ==============
ступень                 откуда берётся                           ретроспективна
======================  =======================================  ==============
просмотр лендинга       ``analytics_event`` / ``analytics_daily`` нет
просмотр формы          ``analytics_event`` / ``analytics_daily`` нет
аккаунт создан          ``users.created_at``                      ДА
онбординг пройден       kv ``onboarded_<uid>``                    ДА
модель подключена       ``user_settings`` (kv у владельца)        ДА
первое сообщение        ``chat_message`` (role='user')            ДА
======================  =======================================  ==============

Четыре нижние ступени работают за всю историю инстанса, даже если аналитику
включили сегодня. Две верхние — только с момента включения, и отчёт обязан
пометить их как неизмеримые за прошлое, а не рисовать ноль.

Когорта «аккаунт создан» и всё, что ниже, считается ПО ЛЮДЯМ, зарегавшимся в
выбранном окне — иначе конверсия «онбординг / регистрации» смешивала бы
сегодняшних новичков со старожилами и всегда стремилась бы к 100 %.
"""

from __future__ import annotations

from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.analytics.funnel")

#: Пути лендинга. ``/`` у вошедшего владельца — редирект на ``/now``, поэтому
#: обе формы считаются как один вход.
_LANDING_PATHS = ("/landing", "/")
_SIGNUP_PATHS = ("/auth/signup",)


async def _view_hits(day_from: str, day_to: str, paths: tuple[str, ...]) -> int:
    from app.analytics import store  # noqa: PLC0415

    rows = await store.aggregate(day_from, day_to, group=("path",), kind="view")
    return sum(int(r["hits"]) for r in rows if r["path"] in paths)


async def _cohort(day_from: str, day_to: str) -> list[int]:
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT id FROM users "
            "WHERE substr(created_at, 1, 10) >= ? AND substr(created_at, 1, 10) <= ? "
            "ORDER BY id",
            (day_from, day_to),
        )
        return [int(r[0]) for r in await cur.fetchall()]


async def _onboarded(uids: list[int]) -> int:
    if not uids:
        return 0
    placeholders = ", ".join("?" for _ in uids)
    keys = [f"onboarded_{uid}" for uid in uids]
    async with get_connection() as conn:
        cur = await conn.execute(
            # ``placeholders`` — только строка вида "?, ?, ?" по длине uids.
            f"SELECT COUNT(*) FROM kv_settings WHERE key IN ({placeholders}) "  # noqa: S608
            "AND TRIM(COALESCE(value, '')) NOT IN ('', '0')",
            keys,
        )
        row = await cur.fetchone()
    return int(row[0] or 0)


async def configured_llm_ids(
    uids: list[int], owner_id: int | None = None
) -> set[int]:
    """КТО из переданных людей реально подключил модель (множество id).

    У участника ключ и провайдер лежат в ``user_settings``; у владельца — в
    глобальном ``kv_settings`` (см. ``app/llm/client.py``: ``_KV_LLM_PROVIDER``
    и ``_USER_KV_PROVIDER`` — это два разных места, а не одно).
    Признак «подключил» намеренно широкий: выбранный провайдер ИЛИ введённый
    BYO-ключ. Человек, вписавший ключ и не нажавший «сохранить провайдера»,
    модель всё-таки настроил.

    Читается ТОЛЬКО наличие строки: ``value`` проверяется на непустоту прямо в
    SQL и наружу не выносится — это чужой API-ключ, и ему нечего делать ни в
    вызывающем коде, ни тем более в шаблоне.

    Функция возвращает множество, а не число, потому что у неё два потребителя
    с разными вопросами: воронке нужно «сколько» (:func:`_llm_configured`),
    странице «Люди» — «у кого именно» (галочка в строке аккаунта). Определение
    «подключил» при этом остаётся ОДНО: две копии этого предиката разъехались
    бы на первой же правке, и рядом на соседних страницах стояли бы два разных
    числа под одной подписью.
    """
    if not uids:
        return set()
    placeholders = ", ".join("?" for _ in uids)
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT DISTINCT user_id FROM user_settings "  # noqa: S608
            f"WHERE user_id IN ({placeholders}) "
            "  AND (key = 'llm_provider' OR key LIKE 'byo_api_key_%') "
            "  AND TRIM(COALESCE(value, '')) NOT IN ('', 'none')",
            uids,
        )
        found = {int(r[0]) for r in await cur.fetchall()}
        if owner_id is not None and owner_id in uids and owner_id not in found:
            cur = await conn.execute(
                "SELECT TRIM(COALESCE(value, '')) FROM kv_settings "
                "WHERE key = 'llm_provider'"
            )
            row = await cur.fetchone()
            if row and row[0] not in ("", "none"):
                found.add(int(owner_id))
    return found


async def _llm_configured(uids: list[int], owner_id: int | None) -> int:
    """Сколько человек из когорты подключили модель. Предикат — общий."""
    return len(await configured_llm_ids(uids, owner_id))


async def _first_message(uids: list[int]) -> int:
    if not uids:
        return 0
    placeholders = ", ".join("?" for _ in uids)
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT COUNT(DISTINCT s.user_id) FROM chat_session s "  # noqa: S608
            "JOIN chat_message m ON m.session_id = s.id AND m.role = 'user' "
            f"WHERE s.user_id IN ({placeholders})",
            uids,
        )
        row = await cur.fetchone()
    return int(row[0] or 0)


async def build_funnel(
    day_from: str, day_to: str, *, owner_id: int | None = None
) -> dict[str, Any]:
    """Шесть ступеней с абсолютными числами и конверсиями.

    Конверсия считается ДВУМЯ способами и обе показываются:
      * ``step_pct``  — к предыдущей ступени («где именно теряем»);
      * ``total_pct`` — к первой ИЗМЕРИМОЙ ступени («сколько дошло всего»).
    Смешивать их в одну колонку — классический способ нарисовать красивую
    воронку и не заметить, что она врёт.
    """
    uids = await _cohort(day_from, day_to)
    cohort_size = len(uids)
    landing = await _view_hits(day_from, day_to, _LANDING_PATHS)
    signup_form = await _view_hits(day_from, day_to, _SIGNUP_PATHS)

    stages: list[dict[str, Any]] = [
        {
            "key": "landing",
            "title": "Открыли лендинг",
            "count": landing,
            "source": "события аналитики",
            "retroactive": False,
        },
        {
            "key": "signup_form",
            "title": "Дошли до формы регистрации",
            "count": signup_form,
            "source": "события аналитики",
            "retroactive": False,
        },
        {
            "key": "registered",
            "title": "Создали аккаунт",
            "count": cohort_size,
            "source": "users.created_at",
            "retroactive": True,
        },
        {
            "key": "onboarded",
            "title": "Прошли онбординг",
            "count": await _onboarded(uids),
            "source": "kv onboarded_<uid>",
            "retroactive": True,
        },
        {
            "key": "llm",
            "title": "Подключили модель",
            "count": await _llm_configured(uids, owner_id),
            "source": "user_settings / kv llm_provider",
            "retroactive": True,
        },
        {
            "key": "first_message",
            "title": "Написали первое сообщение",
            "count": await _first_message(uids),
            "source": "chat_message",
            "retroactive": True,
        },
    ]

    # Первая ступень с ненулевым счётчиком — база для «сколько дошло всего».
    base = next((s["count"] for s in stages if s["count"]), 0)
    previous: int | None = None
    for stage in stages:
        count = int(stage["count"])
        # Делить на ноль нечем: у ступени с нулевым предшественником конверсии
        # не существует, и прочерк честнее, чем подставленные 0 % или 100 %.
        stage["step_pct"] = round(count * 100.0 / previous, 1) if previous else None
        stage["total_pct"] = round(count * 100.0 / base, 1) if base else None
        previous = count

    return {
        "stages": stages,
        "cohort": cohort_size,
        "day_from": day_from,
        "day_to": day_to,
    }


__all__ = ["build_funnel", "configured_llm_ids"]
