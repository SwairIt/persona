"""Волна 5: IP-allowlist вебхука ЮKassa (defense-in-depth).

Чужой IP → 403 (даже до re-GET платежа). IP из диапазона ЮKassa, пришедший через
доверенный прокси (X-Forwarded-For при peer=127.0.0.1, как за devtunnel) → 200.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.billing import service
from app.storage.db import init_database
from app.web.routes import billing as billing_routes


@pytest_asyncio.fixture
async def client():
    await init_database()
    app = FastAPI()
    app.include_router(billing_routes.router)
    # peer=127.0.0.1 → доверенный прокси, X-Forwarded-For будет учтён (как devtunnel)
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.asyncio
async def test_webhook_rejects_foreign_ip(client, monkeypatch):
    called = {"n": 0}

    async def fake_activate(pid: str):
        called["n"] += 1
        return True

    monkeypatch.setattr(service, "activate_from_payment", fake_activate)

    # XFF с чужим адресом → 403, активация даже не вызывается
    r = await client.post(
        "/billing/webhook",
        json={"object": {"id": "pay_evil"}},
        headers={"X-Forwarded-For": "8.8.8.8"},
    )
    assert r.status_code == 403
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_webhook_accepts_yookassa_ip(client, monkeypatch):
    called = {"pid": None}

    async def fake_activate(pid: str):
        called["pid"] = pid
        return True

    monkeypatch.setattr(service, "activate_from_payment", fake_activate)

    # XFF из диапазона ЮKassa (185.71.76.0/27) → 200, активация вызвана
    r = await client.post(
        "/billing/webhook",
        json={"object": {"id": "pay_ok"}},
        headers={"X-Forwarded-For": "185.71.76.5"},
    )
    assert r.status_code == 200
    assert called["pid"] == "pay_ok"
