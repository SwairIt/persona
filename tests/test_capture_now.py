"""Smoke test for the /api/capture/now manual capture endpoint."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import capture_api

    await init_database()
    app = FastAPI()
    app.include_router(capture_api.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="mss screen capture requires a real display; only stable on Windows test runners",
)
async def test_capture_now_returns_id(client: AsyncClient) -> None:
    resp = await client.post("/api/capture/now")
    if resp.status_code == 500:
        pytest.skip("no display available in this environment")
    assert resp.status_code == 200
    data = resp.json()
    assert "screenshot_id" in data
    assert data["screenshot_id"] > 0
