"""Tests for v0.17 — bulk-pin + saved-search RSS feed."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import aiosqlite
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.settings import get_settings
from app.storage.db import init_database
from app.storage.repository import insert_screenshot
from app.storage.tags import save_search
from app.storage.tiers import count_by_tier


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import pin as pin_routes
    from app.web.routes import rss as rss_routes

    await init_database()
    app = FastAPI()
    app.include_router(pin_routes.router)
    app.include_router(rss_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_bulk_pin_endpoint(client: AsyncClient) -> None:
    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        ids = [
            await insert_screenshot(
                conn,
                captured_at=datetime.now(timezone.utc),
                width=1,
                height=1,
                phash=f"bp{i:014d}",
            )
            for i in range(4)
        ]

    resp = await client.post(
        "/api/screenshots/bulk-pin",
        data={"screenshot_ids": ",".join(str(i) for i in ids)},
    )
    assert resp.status_code == 200
    assert resp.json()["pinned"] == 4

    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        counts = await count_by_tier(conn)
    assert counts.get("pinned", 0) == 4


async def test_bulk_pin_validates(client: AsyncClient) -> None:
    resp = await client.post("/api/screenshots/bulk-pin", data={"screenshot_ids": "  "})
    assert resp.status_code == 400
    resp = await client.post("/api/screenshots/bulk-pin", data={"screenshot_ids": "abc"})
    assert resp.status_code == 400


async def test_saved_search_rss(client: AsyncClient) -> None:
    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await insert_screenshot(
            conn,
            captured_at=datetime.now(timezone.utc),
            width=1,
            height=1,
            phash="ssrss000000000001",
            app_name="VS Code",
            window_title="auth flow refactor",
        )
        sid = await save_search(conn, name="auth-watch", query="auth")

    resp = await client.get(f"/feeds/saved-search/{sid}.rss")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/rss+xml")
    assert "auth-watch" in resp.text
    assert "VS Code" in resp.text


async def test_saved_search_rss_404(client: AsyncClient) -> None:
    resp = await client.get("/feeds/saved-search/99999.rss")
    assert resp.status_code == 404
