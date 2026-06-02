"""Smoke tests for /api/ocr/status and /api/embeddings/status."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import embeddings_status, ocr_status

    await init_database()
    app = FastAPI()
    app.include_router(ocr_status.router)
    app.include_router(embeddings_status.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_ocr_status_returns_zero_counts_on_empty_db(client: AsyncClient) -> None:
    resp = await client.get("/api/ocr/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 0
    assert data["pending"] == 0
    assert "enabled" in data
    assert "progress" in data


async def test_embeddings_status_default_disabled(client: AsyncClient) -> None:
    resp = await client.get("/api/embeddings/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["enabled"] is False
    assert data["candidates"] == 0
    assert data["indexed"] == 0
    assert data["pending"] == 0
