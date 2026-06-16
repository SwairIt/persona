"""Тесты memory-inspector: edit_memory + история/restore (ROADMAP S2a)."""

from __future__ import annotations

import aiosqlite
import pytest

from app.chat.user_memory import (
    add_memory,
    edit_memory,
    invalidate_memory,
    list_memory,
    restore_memory,
)


async def _user(db: aiosqlite.Connection) -> None:
    await db.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'a@b.c','x')")
    await db.commit()


@pytest.mark.asyncio
async def test_edit_memory(db: aiosqlite.Connection) -> None:
    await _user(db)
    mid = await add_memory(1, "живёт в Москве")
    assert await edit_memory(1, mid, "живёт в Питере") is True
    items = await list_memory(1)
    assert items[0]["text"] == "живёт в Питере"
    # пустой текст не применяется
    assert await edit_memory(1, mid, "   ") is False


@pytest.mark.asyncio
async def test_history_split_and_restore(db: aiosqlite.Connection) -> None:
    await _user(db)
    a = await add_memory(1, "работает в стартапе")
    await invalidate_memory(1, a)
    # активные пусты, история содержит факт
    assert await list_memory(1) == []
    alli = await list_memory(1, include_invalidated=True)
    hist = [i for i in alli if i["valid_until"] is not None]
    assert len(hist) == 1 and hist[0]["id"] == a
    # restore возвращает в актуальные
    assert await restore_memory(1, a) is True
    assert len(await list_memory(1)) == 1
