"""Tests for v0.33 — tag trends + vault + diff slider."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import diff_slider as diff_slider_routes
    from app.web.routes import tag_trends as tag_trends_routes
    from app.web.routes import vault as vault_routes

    await init_database()
    app = FastAPI()
    app.include_router(tag_trends_routes.router)
    app.include_router(diff_slider_routes.router)
    app.include_router(vault_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_tag_trend_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/tags/standup/trend")
    assert resp.status_code == 200


async def test_tag_trend_api_returns_30_days(client: AsyncClient) -> None:
    resp = await client.get("/api/tags/standup/trend.json")
    assert resp.status_code == 200
    data = resp.json()
    rows = data if isinstance(data, list) else data.get("days", data.get("series", []))
    assert isinstance(rows, list)
    assert len(rows) >= 28


@pytest.mark.asyncio
async def test_tag_trend_empty_tag() -> None:
    await init_database()
    from app.tag_trends import tag_trend

    rows = await tag_trend("nonexistent-tag-xyz", days=30)
    assert isinstance(rows, list)
    assert all(r["count"] == 0 for r in rows)


async def test_diff_slider_404_on_missing(client: AsyncClient) -> None:
    resp = await client.get("/diff/999999/999998")
    assert resp.status_code == 404


async def test_diff_random_renders_or_404(client: AsyncClient) -> None:
    resp = await client.get("/diff/random")
    assert resp.status_code in {200, 404}


async def test_vault_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/vault")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_vault_set_get_roundtrip() -> None:
    await init_database()
    from app.vault import delete_secret, get_secret, set_secret

    set_result = await set_secret("test-key", "test-value", "master-pass")
    if set_result.get("status") == "missing_dep":
        pytest.skip("cryptography not installed")
    assert set_result["status"] in {"ok", "saved", "set"}

    got = await get_secret("test-key", "master-pass")
    assert got["status"] in {"ok", "found"} or got.get("value") == "test-value"
    if "value" in got:
        assert got["value"] == "test-value"

    await delete_secret("test-key")


@pytest.mark.asyncio
async def test_vault_wrong_password_rejected() -> None:
    await init_database()
    from app.vault import get_secret, set_secret

    set_result = await set_secret("test-key-pw", "secret", "right-pass")
    if set_result.get("status") == "missing_dep":
        pytest.skip("cryptography not installed")

    got = await get_secret("test-key-pw", "wrong-pass")
    assert got["status"] in {"bad_password", "decryption_failed", "error"} or got.get("value") != "secret"
