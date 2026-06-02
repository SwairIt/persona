"""Smoke tests for v0.9 endpoints (mobile, companion, webhooks)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import aiosqlite
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.settings import get_settings
from app.storage.db import init_database
from app.storage.webhooks import list_webhooks


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import (
        companion as companion_routes,
        mobile as mobile_routes,
        webhooks_routes,
    )

    await init_database()
    app = FastAPI()
    app.include_router(mobile_routes.router)
    app.include_router(companion_routes.router)
    app.include_router(webhooks_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_mobile_renders_when_empty(client: AsyncClient) -> None:
    resp = await client.get("/m")
    assert resp.status_code == 200
    assert "Persona" in resp.text


async def test_companion_ingest_valid_tab(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/companion/tab",
        json={"url": "https://example.com/page", "title": "Example"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["domain"] == "example.com"

    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute("SELECT COUNT(*) AS n FROM browser_tabs")
        row = await cursor.fetchone()
        assert int(row["n"]) == 1


async def test_companion_rejects_invalid_url(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/companion/tab",
        json={"url": "not-a-url", "title": "x"},
    )
    assert resp.status_code == 422


async def test_webhooks_create_and_list(client: AsyncClient) -> None:
    resp = await client.post(
        "/webhooks",
        data={
            "url": "https://hook.example.com/persona",
            "event_type": "capture.saved",
            "secret": "shh",
        },
        follow_redirects=False,
    )
    assert resp.status_code in {303, 307}

    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        subs = await list_webhooks(conn)
    assert len(subs) == 1
    assert subs[0]["event_type"] == "capture.saved"


async def test_webhooks_rejects_unknown_event(client: AsyncClient) -> None:
    resp = await client.post(
        "/webhooks",
        data={"url": "https://hook.example.com/", "event_type": "bogus"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


async def test_webhooks_rejects_non_http_url(client: AsyncClient) -> None:
    resp = await client.post(
        "/webhooks",
        data={"url": "ftp://example.com/", "event_type": "capture.saved"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
