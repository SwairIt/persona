"""Бесплатная поверхность УЧАСТНИКА (member-allowlist вместо подписочного гейта).

Контракт MVP: регистрация свободная, биллинг спит. Любой зарегистрированный
не-владелец получает ``_MEMBER_PREFIXES`` (чат/голос/граф/свои настройки), а
личные данные владельца (/now, /timeline, /root, дашборд-инсайты, админка)
остаются закрытыми: HTML → 303 на /chat, JSON → 403 «owner access required».

Отдельно сторожим УТЕЧКУ в /api/graph.json: таблица ``hourly_card`` глобальна
(захват экрана/звука владельца, без user_id) — участник не должен видеть оттуда
ни одного узла, тогда как у владельца они на месте.

kv ``owner_exclusive_mode`` в этих тестах ВЫКЛ (иначе всех не-владельцев паркует
на /pending — это отдельный kill-switch, см. test_owner_exclusive_lockdown.py).
"""

from __future__ import annotations

import aiosqlite
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from httpx import ASGITransport, AsyncClient

from app.auth import owner
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.auth.users import create_user
from app.storage.repository import set_kv
from app.web.middleware import auth_gate
from app.web.middleware.auth_gate import AuthGateMiddleware
from app.web.routes import account as account_routes
from app.web.routes import memory_graph as memory_graph_routes


def _reset_auth_caches() -> None:
    owner._cache["value"] = None
    owner._cache["checked_at"] = 0.0
    owner._fa_cache["value"] = None
    owner._fa_cache["checked_at"] = 0.0
    auth_gate._cache["value"] = False
    auth_gate._cache["checked_at"] = 0.0
    auth_gate._role_gate_cache["value"] = False
    auth_gate._role_gate_cache["checked_at"] = 0.0
    auth_gate._owner_exclusive_cache["value"] = False
    auth_gate._owner_exclusive_cache["checked_at"] = 0.0


def _app() -> FastAPI:
    """Гейт + реальные member-роуты (account/graph) + заглушки остальных зон.

    HTML-страницы владельца и участника подменены заглушками: проверяем решение
    ГЕЙТА, а не рендер шаблонов. /api/account.json и /api/graph.json — настоящие
    роутеры (именно их поведение для участника здесь и сторожим).
    """
    app = FastAPI()
    app.add_middleware(AuthGateMiddleware)
    app.include_router(account_routes.router)
    app.include_router(memory_graph_routes.router)

    @app.get("/chat")
    async def _chat(request: Request) -> JSONResponse:
        # заодно отдаём то, что гейт положил в request.state
        return JSONResponse({
            "page": "chat",
            "user_id": getattr(request.state, "user_id", None),
            "is_owner": getattr(request.state, "is_owner", None),
        })

    @app.get("/voice")
    async def _voice() -> PlainTextResponse:
        return PlainTextResponse("VOICE")

    @app.get("/timeline")
    async def _timeline() -> PlainTextResponse:
        return PlainTextResponse("TIMELINE")

    @app.get("/now")
    async def _now() -> PlainTextResponse:
        return PlainTextResponse("NOW")

    @app.get("/root")
    async def _root() -> PlainTextResponse:
        return PlainTextResponse("ROOT")

    @app.get("/api/dashboard/insights.json")
    async def _insights() -> dict[str, bool]:
        return {"insights": True}

    return app


@pytest_asyncio.fixture
async def member_setup(db: aiosqlite.Connection):
    owner_user = await create_user("owner@member.test", "owner-pass-123")
    member_user = await create_user("member@member.test", "member-pass-123")
    await set_kv(db, "owner_user_id", str(owner_user["id"]))
    await set_kv(db, "owner_exclusive_mode", "0")
    _reset_auth_caches()

    app = _app()
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, db, owner_user, member_user
    finally:
        _reset_auth_caches()


async def _as(client: AsyncClient, uid: int) -> None:
    client.cookies.clear()
    token, _ = await issue_session(uid)
    client.cookies.set(SESSION_COOKIE_NAME, token)


@pytest.mark.asyncio
async def test_member_reaches_free_surface(member_setup):
    client, _db, _owner_user, member_user = member_setup
    await _as(client, member_user["id"])

    chat = await client.get("/chat", follow_redirects=False)
    assert chat.status_code == 200
    assert (await client.get("/voice", follow_redirects=False)).status_code == 200
    assert (await client.get("/api/account.json")).status_code == 200


@pytest.mark.asyncio
async def test_owner_only_surface_stays_closed_for_member(member_setup):
    client, _db, _owner_user, member_user = member_setup
    await _as(client, member_user["id"])

    for path in ("/timeline", "/now", "/root"):
        r = await client.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"] == "/chat", path

    api = await client.get("/api/dashboard/insights.json")
    assert api.status_code == 403
    assert api.json()["detail"] == "owner access required"


@pytest.mark.asyncio
async def test_prefix_match_does_not_leak_neighbouring_paths(member_setup):
    """``/settings/llmXXX`` не должен пролезать как ``/settings/llm``."""
    client, _db, _owner_user, member_user = member_setup
    await _as(client, member_user["id"])

    assert auth_gate._is_member_path("/settings/llm") is True
    assert auth_gate._is_member_path("/settings/llm/models") is True
    assert auth_gate._is_member_path("/settings/llmXXX") is False
    assert auth_gate._is_member_path("/chatter") is False
    assert auth_gate._is_member_path("/api/graph.json") is True
    assert auth_gate._is_member_path("/api/graph.jsonx") is False
    # нормализационный обход
    assert auth_gate._is_member_path("/chat/../now") is False


@pytest.mark.asyncio
async def test_account_json_reports_member_not_owner(member_setup):
    client, _db, owner_user, member_user = member_setup

    await _as(client, member_user["id"])
    body = (await client.get("/api/account.json")).json()
    assert body["is_owner"] is False
    assert body["email"] == "member@member.test"

    await _as(client, owner_user["id"])
    body = (await client.get("/api/account.json")).json()
    assert body["is_owner"] is True


@pytest.mark.asyncio
async def test_request_state_identity_is_set_for_owner_and_member(member_setup):
    """Гейт кладёт user_id/is_owner в request.state для ЛЮБОГО аутентифицированного."""
    client, _db, owner_user, member_user = member_setup

    await _as(client, member_user["id"])
    body = (await client.get("/chat")).json()
    assert body["user_id"] == member_user["id"]
    assert body["is_owner"] is False

    await _as(client, owner_user["id"])
    body = (await client.get("/chat")).json()
    assert body["user_id"] == owner_user["id"]
    assert body["is_owner"] is True


@pytest.mark.asyncio
async def test_graph_json_hides_owner_hourly_cards_from_member(member_setup):
    """УТЕЧКА: hourly_card глобальна (без user_id) — участник её видеть не должен."""
    client, db, owner_user, member_user = member_setup
    await db.execute(
        "INSERT INTO hourly_card "
        "(hour_start, hour_end, summary, screen_count, audio_seconds, transcript_excerpt) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            "2026-08-20T10:00:00",
            "2026-08-20T10:59:59",
            "Секретный час владельца",
            12,
            300,
            "владелец говорил вслух",
        ),
    )
    await db.commit()

    await _as(client, member_user["id"])
    member_graph = await client.get("/api/graph.json")
    assert member_graph.status_code == 200
    member_nodes = member_graph.json()["nodes"]
    assert [n for n in member_nodes if n["id"].startswith("h")] == []
    blob = member_graph.text
    assert "Секретный час владельца" not in blob
    assert "владелец говорил вслух" not in blob
    # и никаких ссылок на owner-only страницы
    assert all(
        not str(n.get("href", "")).startswith(("/timeline", "/entity/"))
        for n in member_nodes
    )

    await _as(client, owner_user["id"])
    owner_graph = await client.get("/api/graph.json")
    assert owner_graph.status_code == 200
    owner_hourly = [n for n in owner_graph.json()["nodes"] if n["id"].startswith("h")]
    assert len(owner_hourly) == 1
    assert owner_hourly[0]["type"] == "recording"       # был звук/речь
    assert owner_hourly[0]["href"] == "/timeline?date=2026-08-20"
    # день-узел владельца сохраняет ссылку на таймлайн (байт-в-байт как раньше)
    day_nodes = [n for n in owner_graph.json()["nodes"] if n["type"] == "day"]
    assert day_nodes and day_nodes[0]["href"] == "/timeline?date=2026-08-20"
