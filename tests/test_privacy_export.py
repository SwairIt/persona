"""Тесты приватности: экспорт памяти в Markdown + счётчики (ROADMAP S2b)."""

from __future__ import annotations

import aiosqlite
import pytest

from app.web.routes.privacy_settings import _counts, _export_markdown


@pytest.mark.asyncio
async def test_export_and_counts(db: aiosqlite.Connection) -> None:
    await db.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'a@b.c','x')")
    await db.execute("INSERT INTO user_memory(user_id,kind,text,pinned) VALUES(1,'fact','любит кофе',1)")
    await db.execute("INSERT INTO chat_session(id,user_id,title) VALUES(5,1,'Про деплой')")
    await db.execute("INSERT INTO chat_message(session_id,role,content) VALUES(5,'user','как задеплоить')")
    await db.execute("INSERT INTO chat_message(session_id,role,content) VALUES(5,'assistant','вот так')")
    await db.commit()

    counts = await _counts(1)
    assert counts["facts"] == 1 and counts["messages"] == 2

    md = await _export_markdown(1)
    assert "любит кофе" in md
    assert "Про деплой" in md
    assert "как задеплоить" in md and "вот так" in md
    assert md.startswith("# Persona")
