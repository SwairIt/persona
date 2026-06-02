"""Tests for v0.45 — app icons + encrypted notes + retention preview."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import app_icons as app_icons_routes
    from app.web.routes import encrypted_notes as encrypted_notes_routes
    from app.web.routes import retention_preview as retention_preview_routes

    await init_database()
    app = FastAPI()
    app.include_router(app_icons_routes.router)
    app.include_router(encrypted_notes_routes.router)
    app.include_router(retention_preview_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_app_icon_route_returns_png(client: AsyncClient) -> None:
    resp = await client.get("/app-icon/Slack.png")
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith("image/")
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"


async def test_app_icon_caches_same_bytes(client: AsyncClient) -> None:
    a = await client.get("/app-icon/Slack.png")
    b = await client.get("/app-icon/Slack.png")
    assert a.content == b.content


@pytest.mark.asyncio
async def test_get_icon_png_deterministic_initials() -> None:
    await init_database()
    from app.app_icons import get_icon_png

    a = await get_icon_png("VS Code")
    b = await get_icon_png("VS Code")
    assert a == b
    assert a[:8] == b"\x89PNG\r\n\x1a\n"


async def test_encrypt_404_on_missing_note(client: AsyncClient) -> None:
    resp = await client.post("/api/notes/999999/encrypt", data={"password": "abc"})
    assert resp.status_code in {404, 400, 503}


async def test_retention_preview_page(client: AsyncClient) -> None:
    resp = await client.get("/admin/retention-preview")
    assert resp.status_code == 200


async def test_retention_preview_api_shape(client: AsyncClient) -> None:
    resp = await client.get("/api/retention-preview.json")
    assert resp.status_code == 200
    data = resp.json()
    for key in ("to_demote_warm", "to_demote_cold", "to_hard_delete"):
        assert key in data


@pytest.mark.asyncio
async def test_retention_preview_empty_db() -> None:
    await init_database()
    from app.retention_preview import preview

    result = await preview()
    assert result["to_demote_warm"] == 0
    assert result["to_demote_cold"] == 0
    assert result["to_hard_delete"] == 0
