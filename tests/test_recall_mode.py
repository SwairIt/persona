"""Тесты маршрутизации recall_mode + backfill no-op без sqlite-vec (ROADMAP S1c)."""

from __future__ import annotations

import aiosqlite
import pytest

from app.web.routes.chat_sessions import _get_recall_mode


@pytest.mark.asyncio
async def test_default_keyword_without_sqlite_vec(db: aiosqlite.Connection) -> None:
    # На CI sqlite-vec не установлен → дефолт keyword (безопасный fallback).
    assert await _get_recall_mode() == "keyword"


@pytest.mark.asyncio
async def test_explicit_mode_wins(db: aiosqlite.Connection) -> None:
    from app.storage.db import get_connection
    from app.storage.repository import set_kv

    async with get_connection() as c:
        await set_kv(c, "recall_mode", "smart")
        await c.commit()
    assert await _get_recall_mode() == "smart"


@pytest.mark.asyncio
async def test_default_hybrid_when_vec_available(
    db: aiosqlite.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    # _get_recall_mode импортит sqlite_vec_available из app.storage.db внутри функции.
    monkeypatch.setattr("app.storage.db.sqlite_vec_available", lambda: True)
    assert await _get_recall_mode() == "hybrid"


@pytest.mark.asyncio
async def test_backfill_index_noop_without_vec(db: aiosqlite.Connection) -> None:
    from app.memory_vec import backfill_index

    # без sqlite-vec бэкфилл — безопасный no-op
    assert await backfill_index() == 0
