"""Smoke tests for v0.5 routes (apps, digest, full-export, timeline-api)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.settings import get_settings
from app.storage.db import init_database
from app.storage.repository import insert_screenshot


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.auth import SESSION_COOKIE_NAME, issue_session
    from app.web.routes import (
        app_stats as app_stats_routes,
        digest as digest_routes,
        full_export as full_export_routes,
        timeline_api as timeline_api_routes,
    )

    await init_database()
    # Ф (security, 2026-06-24): full-export zip — owner-only.
    # Создаём владельца + сессию и шлём cookie, иначе 303 на логин.
    async with aiosqlite.connect(get_settings().db_path) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(1,'t@x.c','x')"
        )
        await conn.commit()
    token, _ = await issue_session(1)
    app = FastAPI()
    app.include_router(app_stats_routes.router)
    app.include_router(digest_routes.router)
    app.include_router(full_export_routes.router)
    app.include_router(timeline_api_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={SESSION_COOKIE_NAME: token},
    ) as ac:
        yield ac


async def _seed(app: str = "VS Code") -> int:
    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        return await insert_screenshot(
            conn,
            captured_at=datetime.now(timezone.utc),
            width=10,
            height=10,
            phash="v50001",
            app_name=app,
            window_title="main.py",
        )


@pytest.mark.skip(reason="apps_index.html depends on base.html jinja globals that the isolated test app doesn't wire")
async def test_apps_index_empty(client: AsyncClient) -> None:
    resp = await client.get("/apps")
    assert resp.status_code == 200


@pytest.mark.skip(reason="apps_index.html depends on base.html jinja globals that the isolated test app doesn't wire")
async def test_apps_index_after_seed(client: AsyncClient) -> None:
    await _seed()
    resp = await client.get("/apps")
    assert resp.status_code == 200
    assert "VS Code" in resp.text


async def test_app_detail_404(client: AsyncClient) -> None:
    resp = await client.get("/apps/NotExists")
    assert resp.status_code == 404


async def test_app_detail_ok(client: AsyncClient) -> None:
    await _seed("Slack")
    resp = await client.get("/apps/Slack")
    assert resp.status_code == 200
    assert "Slack" in resp.text


async def test_weekly_digest(client: AsyncClient) -> None:
    resp = await client.get("/digest/weekly")
    assert resp.status_code == 200
    assert "Weekly digest" in resp.text


async def test_full_export_zip(client: AsyncClient) -> None:
    resp = await client.get("/api/export/full.zip")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/zip")


async def test_timeline_new_count(client: AsyncClient) -> None:
    sid = await _seed("App")
    resp = await client.get(f"/api/timeline/new-count?since_id={sid - 1}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["new"] >= 1
    assert data["max_id"] >= sid

    resp = await client.get(f"/api/timeline/new-count?since_id={sid}")
    assert resp.json()["new"] == 0
