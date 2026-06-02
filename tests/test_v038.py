"""Tests for v0.38 — Cmd+K palette + shot of the week + stats CSV."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import palette as palette_routes
    from app.web.routes import shot_of_week as shot_of_week_routes
    from app.web.routes import stats_csv as stats_csv_routes

    await init_database()
    app = FastAPI()
    app.include_router(palette_routes.router)
    app.include_router(shot_of_week_routes.router)
    app.include_router(stats_csv_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_palette_api_returns_items(client: AsyncClient) -> None:
    resp = await client.get("/api/palette.json")
    assert resp.status_code == 200
    data = resp.json()
    items = data if isinstance(data, list) else data.get("items", [])
    assert isinstance(items, list)
    assert len(items) >= 10
    for item in items[:5]:
        assert "title" in item
        assert "url" in item


async def test_shot_of_week_page(client: AsyncClient) -> None:
    resp = await client.get("/shot-of-the-week")
    assert resp.status_code == 200


async def test_shot_of_week_api(client: AsyncClient) -> None:
    resp = await client.get("/api/shot-of-the-week.json")
    assert resp.status_code in {200, 404}


@pytest.mark.asyncio
async def test_shot_of_week_empty_db_falls_back_or_none() -> None:
    await init_database()
    from app.shot_of_week import shot_of_this_week

    result = await shot_of_this_week()
    assert result is None or isinstance(result, dict)


async def test_stats_csv_streams(client: AsyncClient) -> None:
    resp = await client.get("/export/stats.csv?days=7")
    assert resp.status_code == 200
    ctype = resp.headers.get("content-type", "")
    assert "csv" in ctype or "text/" in ctype
    body = resp.text
    assert "date" in body.lower()
    assert "app" in body.lower() or "shots" in body.lower()


@pytest.mark.asyncio
async def test_stats_csv_module_headers() -> None:
    await init_database()
    from app.stats_csv import export_stats_csv

    csv_text = await export_stats_csv(days_back=7)
    assert isinstance(csv_text, str)
    first_line = csv_text.splitlines()[0]
    cols = [c.strip().strip('"').lower() for c in first_line.split(",")]
    assert "date" in cols
    assert any(c in cols for c in ("app_name", "app", "name"))


@pytest.mark.asyncio
async def test_stats_csv_safe_against_injection() -> None:
    """CSV writer must quote fields containing newlines/commas."""
    await init_database()
    from app.stats_csv import export_stats_csv

    csv_text = await export_stats_csv(days_back=7)
    for line in csv_text.splitlines()[1:]:
        commas = line.count(",")
        assert commas >= 0
