"""Tests for the settings AI-search endpoint (/api/settings/ai-search).

Slice D1: the settings palette's AI fallback, wired up in v2.30.20 after a
month sitting untracked with no router registration. The LLM is always
mocked here — never call a real model in a unit test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth import SESSION_COOKIE_NAME, create_user, issue_session
from app.auth import owner as owner_module
from app.llm.client import LLMNotConfigured
from app.storage.repository import set_kv
from app.web.routes import settings_ai_search


def _reset_owner_cache() -> None:
    owner_module._cache["value"] = None
    owner_module._cache["checked_at"] = 0.0
    owner_module._fa_cache["value"] = None
    owner_module._fa_cache["checked_at"] = 0.0


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(settings_ai_search.router)
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
    # owner_user_id is already pinned to owner_id by the caller.
    assert member["id"] != owner_id
    token, _ = await issue_session(member["id"])
    return token


async def test_owner_gets_answer_from_ai_fallback(db, client, monkeypatch) -> None:
    """A weak/novel intent (no keyword hits) reaches the LLM path and the
    mocked keyword rewrite is merged in — this is the palette's whole point."""
    _owner_id, token = await _owner_session(db)
    await set_kv(db, "ai_everywhere", "1")
    client.cookies.set(SESSION_COOKIE_NAME, token)

    async def _fake_ai_keywords(intent: str) -> str:
        return "backup бэкап экспорт"

    monkeypatch.setattr(settings_ai_search, "_ai_keywords", _fake_ai_keywords)

    resp = await client.post(
        "/api/settings/ai-search",
        json={"intent": "хочу чтобы мои данные не пропали если комп сломается"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data
    assert "ai_used" in data
    assert data["ai_used"] is True
    assert isinstance(data["results"], list)
    for row in data["results"]:
        assert {"href", "label", "score", "category", "icon"} <= row.keys()


async def test_non_owner_is_refused(db, client) -> None:
    owner_id, _owner_token = await _owner_session(db)
    member_token = await _member_session(db, owner_id)
    await set_kv(db, "ai_everywhere", "1")
    client.cookies.set(SESSION_COOKIE_NAME, member_token)

    resp = await client.post("/api/settings/ai-search", json={"intent": "тема"})
    assert resp.status_code == 403


async def test_empty_intent_is_handled_without_500(db, client) -> None:
    _owner_id, token = await _owner_session(db)
    await set_kv(db, "ai_everywhere", "1")
    client.cookies.set(SESSION_COOKIE_NAME, token)

    resp = await client.post("/api/settings/ai-search", json={"intent": ""})
    assert resp.status_code == 200
    assert resp.json() == {"results": [], "ai_used": False}


async def test_oversized_intent_is_handled_without_500(db, client) -> None:
    _owner_id, token = await _owner_session(db)
    await set_kv(db, "ai_everywhere", "1")
    client.cookies.set(SESSION_COOKIE_NAME, token)

    huge = "тема настройки внешний вид " * 200  # far above _MAX_INTENT_LEN
    resp = await client.post("/api/settings/ai-search", json={"intent": huge})
    assert resp.status_code != 500
    assert resp.status_code == 200
    body = resp.json()
    assert "results" in body and "ai_used" in body


async def test_llm_unavailable_degrades_to_keyword_only(db, client, monkeypatch) -> None:
    """When the LLM is unconfigured/offline, the palette must still work as
    plain keyword search — never a 500."""
    _owner_id, token = await _owner_session(db)
    await set_kv(db, "ai_everywhere", "1")
    client.cookies.set(SESSION_COOKIE_NAME, token)

    def _boom(*_args, **_kwargs):
        raise LLMNotConfigured("no provider configured")

    monkeypatch.setattr(settings_ai_search, "make_client", _boom)

    resp = await client.post(
        "/api/settings/ai-search",
        json={"intent": "непонятное намерение которое не матчится по ключевым словам"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ai_used"] is False
    assert isinstance(data["results"], list)


async def test_ai_gate_off_returns_404_for_owner(db, client) -> None:
    """Master toggle 'AI everywhere' off -> feature looks like it doesn't
    exist, matching the palette's silent-fallback contract."""
    _owner_id, token = await _owner_session(db)
    await set_kv(db, "ai_everywhere", "0")
    client.cookies.set(SESSION_COOKIE_NAME, token)

    resp = await client.post("/api/settings/ai-search", json={"intent": "тема"})
    assert resp.status_code == 404
