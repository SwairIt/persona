"""Тесты изоляции delete/edit сообщений по user_id — нельзя трогать чужое."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.chat.sessions import append_message, create_session, list_messages
from app.storage.db import init_database
from app.web.routes import chat_sessions as chat_routes


async def _add_user(db, email: str) -> int:
    cur = await db.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, "x")
    )
    await db.commit()
    return int(cur.lastrowid)


@pytest_asyncio.fixture
async def client():
    await init_database()
    app = FastAPI()
    app.include_router(chat_routes.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_cannot_delete_or_edit_others_message(client, db):
    a = await _add_user(db, "a@example.io")
    b = await _add_user(db, "b@example.io")
    sa = await create_session(a, "A")
    sb = await create_session(b, "B")
    ma = await append_message(sa["id"], "user", "secret A")
    mb = await append_message(sb["id"], "user", "secret B")

    token, _ = await issue_session(a)
    client.cookies.set(SESSION_COOKIE_NAME, token)

    # A НЕ может удалить сообщение B
    r = await client.request("DELETE", f"/api/chat/messages/{mb['id']}")
    assert r.status_code == 404
    # A НЕ может отредактировать сообщение B
    r = await client.patch(f"/api/chat/messages/{mb['id']}", json={"content": "hacked"})
    assert r.status_code == 404
    # сообщение B цело
    msgs_b = await list_messages(sb["id"])
    assert any(m["id"] == mb["id"] and m["content"] == "secret B" for m in msgs_b)

    # A МОЖЕТ удалить своё
    r = await client.request("DELETE", f"/api/chat/messages/{ma['id']}")
    assert r.status_code == 200
    msgs_a = await list_messages(sa["id"])
    assert not any(m["id"] == ma["id"] for m in msgs_a)


@pytest.mark.asyncio
async def test_can_edit_own_user_message(client, db):
    a = await _add_user(db, "owner-edit@example.io")
    sa = await create_session(a, "A")
    ma = await append_message(sa["id"], "user", "original")
    token, _ = await issue_session(a)
    client.cookies.set(SESSION_COOKIE_NAME, token)

    r = await client.patch(f"/api/chat/messages/{ma['id']}", json={"content": "edited text"})
    assert r.status_code == 200
    msgs = await list_messages(sa["id"])
    assert any(m["id"] == ma["id"] and m["content"] == "edited text" for m in msgs)
