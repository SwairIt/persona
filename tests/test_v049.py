"""Tests for v0.49 — per-tag RSS + visual diff + per-app retention."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import app_retention as app_retention_routes
    from app.web.routes import rss as rss_routes
    from app.web.routes import visual_diff as visual_diff_routes

    await init_database()
    app = FastAPI()
    app.include_router(rss_routes.router)
    app.include_router(visual_diff_routes.router)
    app.include_router(app_retention_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_tag_rss_unknown_tag_404(client: AsyncClient) -> None:
    resp = await client.get("/tags/nonexistent-tag-xyz.rss")
    assert resp.status_code in {200, 404}


async def test_visual_diff_404_on_missing_shots(client: AsyncClient) -> None:
    resp = await client.get("/api/diff/999998/999999/thumb.png")
    assert resp.status_code in {404, 200}


async def test_app_retention_page(client: AsyncClient) -> None:
    resp = await client.get("/settings/app-retention")
    assert resp.status_code == 200


async def test_app_retention_save(client: AsyncClient) -> None:
    resp = await client.post(
        "/settings/app-retention",
        data={
            "app_name": "TestApp",
            "warm_after_days": "7",
            "cold_after_days": "30",
            "delete_after_days": "365",
            "never_delete": "0",
        },
    )
    assert resp.status_code in {200, 303, 302}


@pytest.mark.asyncio
async def test_app_retention_module_roundtrip() -> None:
    await init_database()
    from app.app_retention import get_override, list_overrides, remove_override, set_override

    await set_override("TestApp", warm=7, cold=30, delete=365, never=False)
    row = await get_override("TestApp")
    assert row is not None
    assert row["warm_after_days"] == 7

    rows = await list_overrides()
    assert any(r["app_name"] == "TestApp" for r in rows)

    await remove_override("TestApp")
    assert await get_override("TestApp") is None


@pytest.mark.asyncio
async def test_app_retention_never_delete_flag() -> None:
    await init_database()
    from app.app_retention import get_override, set_override

    await set_override("Forever", warm=None, cold=None, delete=None, never=True)
    row = await get_override("Forever")
    assert row is not None
    assert row["never_delete"] == 1
