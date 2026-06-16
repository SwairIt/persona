"""Тесты write_transaction (BEGIN IMMEDIATE): конкуренция, откат, целостность."""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from app.storage.db import get_connection, write_transaction


@pytest.mark.asyncio
async def test_write_transaction_commits(db: aiosqlite.Connection) -> None:
    async with write_transaction() as conn:
        await conn.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(1,'a@b.c','x')"
        )
    async with get_connection() as c:
        cur = await c.execute("SELECT email FROM users WHERE id=1")
        row = await cur.fetchone()
    assert row is not None and row["email"] == "a@b.c"


@pytest.mark.asyncio
async def test_write_transaction_rolls_back_on_error(db: aiosqlite.Connection) -> None:
    with pytest.raises(RuntimeError):
        async with write_transaction() as conn:
            await conn.execute(
                "INSERT INTO users(id,email,password_hash) VALUES(2,'r@b.c','x')"
            )
            raise RuntimeError("boom")
    async with get_connection() as c:
        cur = await c.execute("SELECT 1 FROM users WHERE id=2")
        assert await cur.fetchone() is None  # откатилось


@pytest.mark.asyncio
async def test_concurrent_writes_no_deadlock(db: aiosqlite.Connection) -> None:
    """20 одновременных BEGIN IMMEDIATE записей — все коммитятся, без дедлока/BUSY."""
    async with write_transaction() as conn:
        await conn.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'o@b.c','x')")
        await conn.execute("INSERT INTO chat_session(id,user_id,title) VALUES(1,1,'t')")

    async def one(i: int) -> None:
        async with write_transaction() as conn:
            await conn.execute(
                "INSERT INTO chat_message(session_id,role,content) VALUES(1,'user',?)",
                (f"msg {i}",),
            )

    await asyncio.gather(*[one(i) for i in range(20)])
    async with get_connection() as c:
        cur = await c.execute("SELECT COUNT(*) AS n FROM chat_message WHERE session_id=1")
        row = await cur.fetchone()
    assert int(row["n"]) == 20


@pytest.mark.asyncio
async def test_user_memory_roundtrip_via_write_tx(db: aiosqlite.Connection) -> None:
    """user_memory add/list/forget на write_transaction — данные целы."""
    from app.chat.user_memory import add_memory, forget, list_memory, set_pinned

    await db.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'a@b.c','x')")
    await db.commit()
    mid = await add_memory(1, "любит кофе по утрам")
    assert mid
    await add_memory(1, "любит кофе по утрам")  # дедуп — не дублируется
    assert await set_pinned(1, mid, True)
    items = await list_memory(1)
    assert len(items) == 1 and items[0]["pinned"] is True
    assert await forget(1, "кофе") == 1
    assert await list_memory(1) == []
