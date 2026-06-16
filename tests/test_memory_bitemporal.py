"""Тесты bi-temporal памяти + mem0-разрешения противоречий (ROADMAP S1a)."""

from __future__ import annotations

import aiosqlite
import pytest

from app.chat.user_memory import (
    add_memory,
    count_memory,
    invalidate_memory,
    list_memory,
    reconcile_and_add,
    restore_memory,
    search_memory,
)


async def _user(db: aiosqlite.Connection, uid: int = 1) -> None:
    await db.execute("INSERT INTO users(id,email,password_hash) VALUES(?,?,?)", (uid, f"{uid}@x.c", "x"))
    await db.commit()


@pytest.mark.asyncio
async def test_soft_invalidate_hides_from_list_and_search(db: aiosqlite.Connection) -> None:
    await _user(db)
    mid = await add_memory(1, "живёт в Москве")
    assert len(await list_memory(1)) == 1
    assert await count_memory(1) == 1
    assert await invalidate_memory(1, mid) is True
    # ушло из актуальных, но история есть
    assert await list_memory(1) == []
    assert await count_memory(1) == 0
    assert await search_memory(1, "Москве") == []
    hist = await list_memory(1, include_invalidated=True)
    assert len(hist) == 1 and hist[0]["valid_until"] is not None


@pytest.mark.asyncio
async def test_restore(db: aiosqlite.Connection) -> None:
    await _user(db)
    mid = await add_memory(1, "любит кофе")
    await invalidate_memory(1, mid)
    assert await list_memory(1) == []
    assert await restore_memory(1, mid) is True
    assert len(await list_memory(1)) == 1


@pytest.mark.asyncio
async def test_reconcile_update_supersedes(db: aiosqlite.Connection) -> None:
    """UPDATE: старый факт soft-invalidate + новый со ссылкой superseded_by."""
    await _user(db)
    old = await add_memory(1, "живёт в Москве", kind="fact")

    async def decider(_new_text, candidates):
        return {"action": "update", "target_id": old}

    res = await reconcile_and_add(1, "переехал в Берлин", kind="fact", decider=decider)
    assert res["action"] == "update" and res["invalidated"] == old
    active = await list_memory(1)
    assert len(active) == 1 and "Берлин" in active[0]["text"]
    # старый в истории, помечен superseded_by на новый
    hist = await list_memory(1, include_invalidated=True)
    old_row = [h for h in hist if h["id"] == old][0]
    assert old_row["valid_until"] is not None and old_row["superseded_by"] == res["id"]


@pytest.mark.asyncio
async def test_reconcile_noop_on_exact_dup(db: aiosqlite.Connection) -> None:
    await _user(db)
    mid = await add_memory(1, "любит кофе по утрам")
    res = await reconcile_and_add(1, "любит кофе по утрам")
    assert res["action"] == "noop" and res["id"] == mid
    assert len(await list_memory(1)) == 1


@pytest.mark.asyncio
async def test_reconcile_fallback_adds_without_llm(db: aiosqlite.Connection) -> None:
    """Без LLM (decider возвращает None) — обычный ADD независимого факта."""
    await _user(db)
    await add_memory(1, "работает программистом")

    async def no_llm(_t, _c):
        return None

    res = await reconcile_and_add(1, "увлекается бегом", decider=no_llm)
    assert res["action"] == "add"
    assert len(await list_memory(1)) == 2
