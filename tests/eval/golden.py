"""Golden-eval для recall по памяти чатов — измеряет качество ДО/ПОСЛЕ изменений.

Зачем: любое изменение recall/памяти (вектор, RRF, реранкер, mem0) надо мерить на
СВОИХ данных, а не на англоязычных бенчмарках (они не переносятся на RU + источник
«экран+аудио»). Здесь — синтетический, но репрезентативный РУССКИЙ корпус + набор
запросов с ожидаемыми подстроками; функция ``measure`` гоняет любой recall-движок и
считает hit-rate / recall@k.

Запуск на СВОИХ реальных данных: указать PERSONA_DB_PATH на боевую БД и звать
``measure(recall_relevant, user_id=<твой>)`` из своего скрипта — корпус не сеять.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import aiosqlite

# Корпус: (заголовок сессии, [(роль, текст)]). Подобрано так, чтобы keyword/FTS5
# (префиксный матч token*) давал осмысленный baseline на русском.
_CORPUS: list[tuple[str, list[tuple[str, str]]]] = [
    ("Работа: деплой", [
        ("user", "сегодня настраивал деплой проекта Persona на сервере"),
        ("assistant", "ок, деплой Persona зафиксировал — прод обновлён"),
    ]),
    ("Люди: Иван Петров", [
        ("user", "созванивался с Иваном Петровым по бюджету на маркетинг"),
        ("assistant", "запомнил: Иван Петров отвечает за маркетинг и бюджет"),
    ]),
    ("Личное: кофе", [
        ("user", "я обожаю кофе по утрам, без него не человек"),
        ("assistant", "понял, кофе утром — это святое"),
    ]),
    ("Планы: Берлин", [
        ("user", "в следующем месяце планирую переезд в Берлин"),
        ("assistant", "Берлин — отличный выбор, помогу с планированием переезда"),
    ]),
    ("Проект: дедлайн", [
        ("user", "дедлайн по лендингу в пятницу, надо успеть"),
        ("assistant", "дедлайн лендинга в пятницу — держим в фокусе"),
    ]),
    ("Здоровье: спорт", [
        ("user", "начал бегать по утрам, пять километров"),
        ("assistant", "бег по утрам — супер, пять километров это сильно"),
    ]),
]

# (запрос, [подстроки которые ДОЛЖНЫ встретиться в выдаче recall]).
_QUERIES: list[tuple[str, list[str]]] = [
    ("что там с деплоем Persona", ["деплой"]),
    ("кто такой Иван Петров", ["Петров"]),
    ("что я люблю по утрам", ["кофе"]),
    ("куда я переезжаю", ["Берлин"]),
    ("когда дедлайн по лендингу", ["дедлайн"]),
    ("чем я занимаюсь для здоровья", ["бег"]),
]

RecallFn = Callable[..., Awaitable[str]]


async def seed_corpus(conn: aiosqlite.Connection, user_id: int = 1) -> None:
    """Засеять фикс-корпус в scratch-БД (для офлайн-замера baseline)."""
    await conn.execute(
        "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(?,?,?)",
        (user_id, f"eval{user_id}@x.local", "x"),
    )
    for title, msgs in _CORPUS:
        cur = await conn.execute(
            "INSERT INTO chat_session(user_id,title) VALUES(?,?)", (user_id, title)
        )
        sid = cur.lastrowid
        for role, content in msgs:
            await conn.execute(
                "INSERT INTO chat_message(session_id,role,content) VALUES(?,?,?)",
                (sid, role, content),
            )
    await conn.commit()


async def measure(recall_fn: RecallFn, user_id: int = 1, k: int = 6,
                  queries: list[tuple[str, list[str]]] | None = None) -> dict[str, Any]:
    """Прогнать recall_fn по запросам → метрики. recall_fn(user_id, q, limit=k)->str."""
    queries = queries or _QUERIES
    hits = 0
    per_query: list[dict[str, Any]] = []
    for q, expect in queries:
        try:
            block = (await recall_fn(user_id, q, limit=k)) or ""
        except TypeError:
            block = (await recall_fn(user_id, q)) or ""
        low = block.lower()
        found = [e for e in expect if e.lower() in low]
        ok = len(found) == len(expect)
        hits += 1 if ok else 0
        per_query.append({"q": q, "ok": ok, "found": found, "expect": expect})
    n = len(queries)
    return {
        "n": n,
        "hits": hits,
        "hit_rate": round(hits / n, 3) if n else 0.0,
        "per_query": per_query,
    }


__all__ = ["seed_corpus", "measure"]
