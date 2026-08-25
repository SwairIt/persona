"""Tests for the Telegram chats settings page
(``GET/POST /settings/telegram-chats``) — Task 3 of the same plan that added
``telegram_chat_pref`` (Task 1) and owner-selected ingest (Task 2).

Follows the owner/member client convention already used in
``tests/test_thinking_web.py``: a thin app with just this router, a real
owner session cookie, and a separate non-owner session to prove the 403 gate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth import SESSION_COOKIE_NAME, create_user, issue_session
from app.auth import owner as owner_module
from app.integrations.telegram.people import TelegramPeopleRepository
from app.integrations.telegram.repository import TelegramRepository
from app.storage.repository import set_kv
from app.web.routes import telegram_chats


def _reset_owner_cache() -> None:
    owner_module._cache["value"] = None
    owner_module._cache["checked_at"] = 0.0
    owner_module._fa_cache["value"] = None
    owner_module._fa_cache["checked_at"] = 0.0


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(telegram_chats.router)
    return app


@pytest_asyncio.fixture
async def client(db) -> AsyncIterator[AsyncClient]:
    _reset_owner_cache()
    app = _app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    _reset_owner_cache()


async def _owner_session(db) -> tuple[int, str]:
    owner = await create_user("owner@example.test", "Zq7-frost-lantern-91")
    await set_kv(db, "owner_user_id", str(owner["id"]))
    token, _ = await issue_session(owner["id"])
    return owner["id"], token


async def _member_session(db, owner_id: int) -> str:
    member = await create_user("member@example.test", "Kp4-velvet-harbour-38")
    assert member["id"] != owner_id
    token, _ = await issue_session(member["id"])
    return token


async def _seen_chat(db, persona_user_id: int, chat_id: int) -> None:
    people = TelegramPeopleRepository()
    await people.observe_message(
        persona_user_id=persona_user_id,
        owner_telegram_user_id=100,
        sender={"id": 100, "first_name": "Владелец"},
        chat_id=chat_id,
        message_id=1,
        text="привет",
    )


async def test_page_lists_a_chat_persona_has_seen(db, client) -> None:
    owner_id, token = await _owner_session(db)
    await _seen_chat(db, owner_id, -111)
    client.cookies.set(SESSION_COOKIE_NAME, token)

    resp = await client.get("/settings/telegram-chats")
    assert resp.status_code == 200
    assert "-111" in resp.text


async def test_post_sets_mode_and_ingest_and_redirects(db, client) -> None:
    owner_id, token = await _owner_session(db)
    await _seen_chat(db, owner_id, -222)
    client.cookies.set(SESSION_COOKIE_NAME, token)

    resp = await client.post(
        "/settings/telegram-chats/-222",
        data={"title": "тест-группа", "mode": "reply", "ingest": "on"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings/telegram-chats"

    telegram = TelegramRepository()
    pref = await telegram.chat_pref(-222)
    assert pref is not None
    assert pref["mode"] == "reply"
    assert pref["ingest"] is True
    assert pref["title"] == "тест-группа"
    assert -222 in await telegram.allowed_chat_ids()


async def test_post_unchecked_ingest_box_is_absent_and_treated_as_false(db, client) -> None:
    owner_id, token = await _owner_session(db)
    await _seen_chat(db, owner_id, -333)
    client.cookies.set(SESSION_COOKIE_NAME, token)

    # An unchecked checkbox is simply absent from the body -- no "ingest" key.
    resp = await client.post(
        "/settings/telegram-chats/-333",
        data={"title": "", "mode": "read"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    telegram = TelegramRepository()
    pref = await telegram.chat_pref(-333)
    assert pref is not None
    assert pref["ingest"] is False


async def test_non_owner_gets_403_on_get_and_post(db, client) -> None:
    owner_id, _owner_token = await _owner_session(db)
    await _seen_chat(db, owner_id, -444)
    member_token = await _member_session(db, owner_id)
    client.cookies.set(SESSION_COOKIE_NAME, member_token)

    get_resp = await client.get("/settings/telegram-chats")
    assert get_resp.status_code == 403

    post_resp = await client.post(
        "/settings/telegram-chats/-444",
        data={"title": "", "mode": "reply", "ingest": "on"},
    )
    assert post_resp.status_code == 403


async def test_unknown_chat_id_is_rejected(db, client) -> None:
    owner_id, token = await _owner_session(db)
    client.cookies.set(SESSION_COOKIE_NAME, token)

    resp = await client.post(
        "/settings/telegram-chats/-999999",
        data={"title": "", "mode": "reply", "ingest": "on"},
    )
    assert resp.status_code == 404

    telegram = TelegramRepository()
    assert await telegram.chat_pref(-999999) is None
