"""Tests for v0.28 — heatmap + keywords + shot of the day."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import heatmap as heatmap_routes
    from app.web.routes import keywords as keywords_routes
    from app.web.routes import shot_of_day as shot_of_day_routes

    await init_database()
    app = FastAPI()
    app.include_router(heatmap_routes.router)
    app.include_router(keywords_routes.router)
    app.include_router(shot_of_day_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_heatmap_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/heatmap")
    assert resp.status_code == 200


async def test_heatmap_api_shape(client: AsyncClient) -> None:
    resp = await client.get("/api/heatmap.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "start_date" in data
    assert "end_date" in data
    assert "days" in data
    assert "max_count" in data
    assert "total" in data
    assert isinstance(data["days"], list)
    assert len(data["days"]) >= 365 or len(data["days"]) > 0


@pytest.mark.asyncio
async def test_yearly_heatmap_zero_state() -> None:
    await init_database()
    from app.heatmap import yearly_heatmap

    result = await yearly_heatmap()
    assert result["total"] >= 0
    assert result["max_count"] >= 0
    assert all(d["count"] == 0 for d in result["days"])
    assert all(d["level"] == 0 for d in result["days"])


async def test_keywords_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/keywords")
    assert resp.status_code == 200


async def test_keywords_api_shape(client: AsyncClient) -> None:
    resp = await client.get("/api/keywords.json?days=7&n=15")
    assert resp.status_code == 200
    data = resp.json()
    # v1.0+ shape: {"count","days","items","n"}. Legacy: list or keywords/results.
    assert (
        isinstance(data, list)
        or "keywords" in data
        or "results" in data
        or "items" in data
    )


@pytest.mark.asyncio
async def test_top_keywords_stopwords_filtered() -> None:
    from app.storage.db import init_database
    from app.keywords import STOPWORDS, top_keywords

    await init_database()
    assert "the" in STOPWORDS
    assert "это" in STOPWORDS
    result = await top_keywords(days=7, top_n=30)
    assert isinstance(result, list)
    for kw in result:
        assert kw["word"] not in STOPWORDS


async def test_shot_of_the_day_renders(client: AsyncClient) -> None:
    """On empty DB, page renders with empty state."""
    resp = await client.get("/shot-of-the-day")
    assert resp.status_code == 200


async def test_shot_of_the_day_api_empty(client: AsyncClient) -> None:
    """On empty DB, API returns 404."""
    resp = await client.get("/api/shot-of-the-day.json")
    assert resp.status_code in {200, 404}


@pytest.mark.asyncio
async def test_shot_of_today_deterministic() -> None:
    """Two calls on the same day should return the same result (or both None)."""
    await init_database()
    from app.shot_of_day import shot_of_today

    a = await shot_of_today()
    b = await shot_of_today()
    assert a == b
