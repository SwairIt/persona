"""Tests for v0.42 — day scrubber + OCR retry queue + day collage PNG."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import day_collage as day_collage_routes
    from app.web.routes import day_scrubber as day_scrubber_routes
    from app.web.routes import ocr_retry as ocr_retry_routes

    await init_database()
    app = FastAPI()
    app.include_router(day_scrubber_routes.router)
    app.include_router(ocr_retry_routes.router)
    app.include_router(day_collage_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_scrubber_page(client: AsyncClient) -> None:
    resp = await client.get("/scrubber/2026-06-02")
    assert resp.status_code == 200


async def test_scrubber_api_returns_list(client: AsyncClient) -> None:
    resp = await client.get("/api/scrubber/2026-06-02.json")
    assert resp.status_code == 200
    data = resp.json()
    items = data if isinstance(data, list) else data.get("items", data.get("shots", []))
    assert isinstance(items, list)


async def test_ocr_retry_page(client: AsyncClient) -> None:
    resp = await client.get("/admin/ocr-retry")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_ocr_retry_list_empty_db() -> None:
    await init_database()
    from app.ocr_retry import list_problem_shots

    rows = await list_problem_shots(limit=10)
    assert isinstance(rows, list)


@pytest.mark.asyncio
async def test_ocr_retry_requeue_empty_list() -> None:
    await init_database()
    from app.ocr_retry import requeue_shots

    count = await requeue_shots([])
    assert count == 0


async def test_collage_route_empty_day(client: AsyncClient) -> None:
    """Empty day — endpoint may return 200 with empty PNG or 404."""
    resp = await client.get("/export/collage.png?day=2099-01-01")
    assert resp.status_code in {200, 404}


@pytest.mark.asyncio
async def test_build_day_collage_empty(tmp_path) -> None:
    await init_database()
    from app.day_collage import build_day_collage

    out = tmp_path / "collage.png"
    result = await build_day_collage("2099-01-01", out, cols=4, max_shots=24)
    assert result["status"] in {"ok", "empty"}
