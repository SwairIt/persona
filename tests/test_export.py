"""Tests for day-snapshot export endpoints."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database
from app.storage.repository import insert_screenshot


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import export

    await init_database()
    app = FastAPI(title="Persona-Export-Test")
    app.include_router(export.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _seed_day(target: datetime, n: int = 3) -> None:
    base = target.replace(hour=10, minute=0, second=0, microsecond=0, tzinfo=timezone.utc)
    import aiosqlite

    from app.settings import get_settings

    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        for i in range(n):
            await insert_screenshot(
                conn,
                captured_at=base + timedelta(minutes=i * 5),
                width=100,
                height=100,
                phash=f"e{i:015d}",
                app_name=f"App{i}",
                window_title=f"Window {i}",
            )


async def test_export_day_returns_json(client: AsyncClient) -> None:
    today = datetime(2026, 5, 1)
    await _seed_day(today, n=4)

    resp = await client.get(f"/api/export/day?date={today.strftime('%Y-%m-%d')}")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert "attachment" in resp.headers["content-disposition"]

    data = json.loads(resp.text)
    assert data["day"] == "2026-05-01"
    assert data["count"] == 4
    assert len(data["screenshots"]) == 4
    assert data["screenshots"][0]["app_name"].startswith("App")


async def test_export_day_no_data(client: AsyncClient) -> None:
    resp = await client.get("/api/export/day?date=2099-01-01")
    assert resp.status_code == 200
    data = json.loads(resp.text)
    assert data["count"] == 0
