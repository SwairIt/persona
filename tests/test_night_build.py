"""Тесты ночного билда 2026-06-24: память (salience/scoring/dreams) + безопасность
(SSRF-гард, rate-limit). Покрывают новый код Ф3 (память) и Ф1 (security helpers)."""

from __future__ import annotations

import aiosqlite
import pytest

from app.chat.user_memory import _heuristic_importance, add_memory, list_memory
from app.dreams import add_reflection, invalidate_reflection, list_active_reflections
from app.memory_vec import _jaccard, _minmax, score_and_rerank
from app.net_guard import url_is_safe
from app.web.rate_limit import allow


async def _user(db: aiosqlite.Connection, uid: int = 1) -> None:
    await db.execute(
        "INSERT INTO users(id,email,password_hash) VALUES(?,?,?)", (uid, f"{uid}@x.c", "x")
    )
    await db.commit()


# ── Ф3-C: эвристика важности (чистая) ──────────────────────────────────────
def test_heuristic_importance_gradient() -> None:
    name = _heuristic_importance("Меня зовут Ярослав, делаю Persona", "person")
    smalltalk = _heuristic_importance("привет", "fact")
    pinned_pref = _heuristic_importance("Люблю тёмную тему", "preference", pinned=True)
    assert 1.0 <= smalltalk <= 10.0
    assert name > smalltalk  # durable-факт важнее смолтока
    assert pinned_pref > name  # закреплённое предпочтение — ещё выше
    # кламп в [1,10]
    assert _heuristic_importance("x" * 500, "person", pinned=True) <= 10.0
    assert _heuristic_importance("", "other") >= 1.0


# ── Ф3-C: salience пишется и влияет на отбор фактов ────────────────────────
@pytest.mark.asyncio
async def test_add_memory_sets_salience(db: aiosqlite.Connection) -> None:
    await _user(db)
    high = await add_memory(1, "Меня зовут Ярослав", kind="person")
    low = await add_memory(1, "ок", kind="fact")
    cur = await db.execute(
        "SELECT id, salience, importance_source FROM user_memory WHERE id IN (?,?)",
        (high, low),
    )
    rows = {r["id"]: r for r in await cur.fetchall()}
    assert rows[high]["importance_source"] == "heuristic"
    assert rows[high]["salience"] is not None
    assert rows[high]["salience"] > rows[low]["salience"]


@pytest.mark.asyncio
async def test_list_memory_order_by_salience(db: aiosqlite.Connection) -> None:
    await _user(db)
    await add_memory(1, "ок", kind="fact")  # низкая важность, добавлена ПЕРВОЙ
    await add_memory(1, "Меня зовут Ярослав, важная цель — Persona", kind="person")
    # хронологически первым был бы "ок" (id DESC даёт последний), но по важности
    # первым должен всплыть факт про имя/цель.
    by_sal = await list_memory(1, limit=1, order_by_salience=True)
    assert "Ярослав" in by_sal[0]["text"]


# ── Ф3-B: scoring-хелперы (чистые) ─────────────────────────────────────────
def test_minmax() -> None:
    assert _minmax([1.0, 2.0, 3.0]) == [0.0, 0.5, 1.0]
    assert _minmax([5.0, 5.0]) == [0.5, 0.5]  # вырожденный → нейтраль
    assert _minmax([]) == []


def test_jaccard() -> None:
    assert _jaccard("привет мир", "привет мир") == 1.0
    assert _jaccard("кошка собака", "слон кит") == 0.0
    mid = _jaccard("красная машина быстрая", "красная машина медленная")
    assert 0.0 < mid < 1.0


@pytest.mark.asyncio
async def test_score_and_rerank_never_crashes(db: aiosqlite.Connection) -> None:
    # пустой / одиночный набор → возвращается как есть (recall не падает из-за скоринга)
    assert await score_and_rerank([]) == []
    one = [{"message_id": 1, "content": "x"}]
    assert await score_and_rerank(one) == one


# ── Ф3-D: рефлексии ночного «сна» (db round-trip) ──────────────────────────
@pytest.mark.asyncio
async def test_reflections_roundtrip(db: aiosqlite.Connection) -> None:
    await _user(db)
    rid = await add_reflection(1, "Пользователь увлечён Persona", kind="insight")
    assert rid is not None
    await add_reflection(1, "За неделю много про память", kind="dream")
    active = await list_active_reflections(1, kinds=["insight", "dream"])
    assert len(active) == 2
    assert {r["kind"] for r in active} == {"insight", "dream"}
    # soft-invalidate убирает из активных, но не из истории
    assert await invalidate_reflection(1, rid) is True
    assert len(await list_active_reflections(1, kinds=["insight", "dream"])) == 1
    # пустой текст не пишется
    assert await add_reflection(1, "   ", kind="insight") is None


# ── Ф1: SSRF-гард (чистая, литеральные IP — без сети) ──────────────────────
def test_url_is_safe_blocks_internal() -> None:
    for bad in (
        "http://127.0.0.1/",
        "http://10.0.0.5/admin",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://[::1]/",
        "http://0.0.0.0/",
        "ftp://example.com/",       # не http(s)
        "file:///etc/passwd",
        "not a url",
        "",
    ):
        assert url_is_safe(bad) is False, bad


def test_url_is_safe_allows_public() -> None:
    # литеральные публичные IP — getaddrinfo резолвит без сетевого DNS
    assert url_is_safe("http://8.8.8.8/") is True
    assert url_is_safe("https://1.1.1.1/path?q=1") is True


# ── Ф1: rate-limit sliding window (чистая) ─────────────────────────────────
def test_rate_limit_allow() -> None:
    key = "test:rate:unique-a"
    assert allow(key, 2, 60) is True   # 1
    assert allow(key, 2, 60) is True   # 2
    assert allow(key, 2, 60) is False  # 3 — сверх лимита
    # другой ключ независим
    assert allow("test:rate:unique-b", 2, 60) is True
