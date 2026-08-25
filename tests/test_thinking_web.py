"""Tests for the thinking-loop settings page and diary
(``/settings/thinking``, ``/thoughts``) — Task 5 of the thinking-loop plan.

Follows the owner/member client convention already used in
``tests/test_settings_ai_search.py``: a thin app with just this router, a
real owner session cookie, and a separate non-owner session to prove the
403 gate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth import SESSION_COOKIE_NAME, create_user, issue_session
from app.auth import owner as owner_module
from app.storage.repository import set_kv
from app.thinking.store import ThoughtStore
from app.web.routes import thinking


def _reset_owner_cache() -> None:
    owner_module._cache["value"] = None
    owner_module._cache["checked_at"] = 0.0
    owner_module._fa_cache["value"] = None
    owner_module._fa_cache["checked_at"] = 0.0


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(thinking.router)
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


async def test_settings_page_renders_and_preselects_stored_values(db, client) -> None:
    owner_id, token = await _owner_session(db)
    await set_kv(db, "thinking_enabled", "true")
    await set_kv(db, "thinking_step_cap", "3")
    await set_kv(db, "thinking_seed_kinds", "know_you,alive")
    await set_kv(db, "thinking_model", "qwen2.5:3b")
    client.cookies.set(SESSION_COOKIE_NAME, token)

    resp = await client.get("/settings/thinking")
    assert resp.status_code == 200
    assert 'value="3"' in resp.text
    assert 'value="qwen2.5:3b"' in resp.text
    assert "know_you" in resp.text


async def test_post_valid_settings_persists_and_redirects(db, client) -> None:
    owner_id, token = await _owner_session(db)
    client.cookies.set(SESSION_COOKIE_NAME, token)

    resp = await client.post(
        "/settings/thinking",
        data={
            "enabled": "on",
            "cap_mode": "fixed",
            "step_cap": "4",
            "emergency_cap": "40",
            "daily_budget": "30",
            "may_write_to_chat": "on",
            "model": "custom-model",
            "seed_kinds": ["know_you", "alive"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/settings/thinking"

    from app.thinking.settings import load_thinking_settings

    saved = await load_thinking_settings()
    assert saved.enabled is True
    assert saved.step_cap == 4
    assert saved.model == "custom-model"
    assert saved.seed_kinds == ("know_you", "alive")


async def test_post_zero_step_cap_is_rejected_and_does_not_change_settings(db, client) -> None:
    owner_id, token = await _owner_session(db)
    await set_kv(db, "thinking_step_cap", "5")
    client.cookies.set(SESSION_COOKIE_NAME, token)

    resp = await client.post(
        "/settings/thinking",
        data={
            "enabled": "on",
            "cap_mode": "fixed",
            "step_cap": "0",
            "emergency_cap": "40",
            "daily_budget": "30",
            "seed_kinds": ["know_you"],
        },
    )
    assert resp.status_code == 400

    from app.thinking.settings import load_thinking_settings

    saved = await load_thinking_settings()
    assert saved.step_cap == 5


async def test_post_with_no_seed_kinds_is_rejected(db, client) -> None:
    owner_id, token = await _owner_session(db)
    client.cookies.set(SESSION_COOKIE_NAME, token)

    resp = await client.post(
        "/settings/thinking",
        data={
            "enabled": "on",
            "cap_mode": "fixed",
            "step_cap": "5",
            "emergency_cap": "40",
            "daily_budget": "30",
        },
    )
    assert resp.status_code == 400


async def test_settings_page_requires_owner(db, client) -> None:
    owner_id, _owner_token = await _owner_session(db)
    member_token = await _member_session(db, owner_id)
    client.cookies.set(SESSION_COOKIE_NAME, member_token)

    resp = await client.get("/settings/thinking")
    assert resp.status_code == 403


async def test_thoughts_page_lists_an_existing_chain(db, client) -> None:
    owner_id, token = await _owner_session(db)
    client.cookies.set(SESSION_COOKIE_NAME, token)

    store = ThoughtStore()
    chain_id = await store.open_chain(
        owner_id,
        seed_text="Что я поняла про владельца сегодня?",
        seed_kind="know_you",
        source_scope="owner_private",
        source_session_id=None,
    )
    await store.close_chain(chain_id, conclusion="Отвечать короче.", certainty="observation")

    resp = await client.get("/thoughts")
    assert resp.status_code == 200
    assert "Отвечать короче." in resp.text
    assert "наблюдение" in resp.text


async def test_thoughts_page_requires_owner(db, client) -> None:
    owner_id, _owner_token = await _owner_session(db)
    member_token = await _member_session(db, owner_id)
    client.cookies.set(SESSION_COOKIE_NAME, member_token)

    resp = await client.get("/thoughts")
    assert resp.status_code == 403


async def test_confirm_writes_memory_once_and_marks_the_thought(db, client) -> None:
    owner_id, token = await _owner_session(db)
    client.cookies.set(SESSION_COOKIE_NAME, token)

    store = ThoughtStore()
    chain_id = await store.open_chain(
        owner_id,
        seed_text="s",
        seed_kind="alive",
        source_scope="owner_private",
        source_session_id=None,
    )
    await store.close_chain(chain_id, conclusion="Владелец любит короткие ответы.")
    steps = await store.chain_steps(chain_id)
    conclusion_id = steps[-1]["id"]

    resp = await client.post(f"/thoughts/{conclusion_id}/confirm", follow_redirects=False)
    assert resp.status_code == 303

    from app.chat.user_memory import list_memory

    memories = await list_memory(owner_id)
    texts = [m["text"] for m in memories]
    assert "Владелец любит короткие ответы." in texts

    # Confirming again must not duplicate the memory fact.
    resp2 = await client.post(f"/thoughts/{conclusion_id}/confirm", follow_redirects=False)
    assert resp2.status_code == 303
    memories_after = await list_memory(owner_id)
    assert len([m for m in memories_after if m["text"] == "Владелец любит короткие ответы."]) == 1
