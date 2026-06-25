"""Тесты кабинета биллинга: автотриал 3 дня, грант Pro, сводка, страница /billing."""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
from app.billing import service
from app.storage.db import init_database
from app.web.routes import billing as billing_routes


@pytest.fixture(autouse=True)
def _reset_owner_cache():
    from app.auth import owner

    owner._cache["value"] = None
    owner._cache["checked_at"] = 0.0
    yield


async def _add_user(db, email: str) -> int:
    cur = await db.execute(
        "INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, "x")
    )
    await db.commit()
    return int(cur.lastrowid)


@pytest.mark.asyncio
async def test_ensure_trial_creates_3day_pro(db):
    uid = await _add_user(db, "trial@example.io")
    await service.ensure_trial(uid)
    s = await service.summary(uid)
    assert s["active"] and s["is_trial"] and s["plan"] == "pro"
    assert 1 <= s["days_left"] <= 3
    assert s["license_key"].startswith("PRSN-")
    # идемпотентно: повторный вызов не пересоздаёт и не меняет ключ
    key = s["license_key"]
    await service.ensure_trial(uid)
    assert (await service.summary(uid))["license_key"] == key


@pytest.mark.asyncio
async def test_grant_pro_max(db):
    uid = await _add_user(db, "max@example.io")
    await service.grant_pro(uid, 3650)
    s = await service.summary(uid)
    assert s["active"] and not s["is_trial"] and s["plan"] == "pro"
    assert s["days_left"] > 3000


@pytest.mark.asyncio
async def test_summary_free_when_no_sub(db):
    uid = await _add_user(db, "free@example.io")
    s = await service.summary(uid)
    assert s["active"] is False and s["plan"] == "free" and s["license_key"] is None


@pytest_asyncio.fixture
async def client():
    await init_database()
    app = FastAPI()
    app.include_router(billing_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_billing_page_shows_license_for_buyer(client, db):
    await _add_user(db, "owner@example.io")          # id=1 → владелец (MIN id)
    buyer = await _add_user(db, "buyer@example.io")   # id=2 → покупатель
    await service.ensure_trial(buyer)
    token, _ = await issue_session(buyer)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    r = await client.get("/billing")
    assert r.status_code == 200
    assert "Триал" in r.text          # бейдж триала
    assert "Войти в приложение" in r.text  # кнопка входа в приложение
    assert "раннем доступе" not in r.text  # старой заглушки больше нет


@pytest.mark.asyncio
async def test_gate_lets_subscriber_into_app(db):
    """Гейт: владелец и активный подписчик → в приложение; без подписки → /billing."""
    from fastapi.responses import PlainTextResponse

    from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
    from app.web.middleware import auth_gate
    from app.web.middleware.auth_gate import AuthGateMiddleware

    owner = await _add_user(db, "o@example.io")    # id=1 → владелец (MIN id)
    sub = await _add_user(db, "s@example.io")       # id=2 → подписчик
    free = await _add_user(db, "f@example.io")      # id=3 → без подписки
    await service.grant_pro(sub, 30)
    auth_gate._cache["checked_at"] = 0.0  # сбросить кэш «гейт активен»

    app = FastAPI()
    app.add_middleware(AuthGateMiddleware)

    @app.get("/now")
    async def _now():
        return PlainTextResponse("APP")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        for uid, expect in ((owner, "app"), (sub, "app"), (free, "billing")):
            ac.cookies.clear()
            token, _ = await issue_session(uid)
            ac.cookies.set(SESSION_COOKIE_NAME, token)
            r = await ac.get("/now", follow_redirects=False)
            if expect == "app":
                assert r.status_code == 200 and r.text == "APP"
            else:
                assert r.status_code == 303 and r.headers["location"] == "/billing"
