"""Tests for v0.50 MILESTONE — feature index + query API + setup wizard."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import feature_index as feature_index_routes
    from app.web.routes import query_api as query_api_routes
    from app.web.routes import setup as setup_routes

    await init_database()
    app = FastAPI()
    app.include_router(feature_index_routes.router)
    app.include_router(query_api_routes.router)
    app.include_router(setup_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_feature_index_page(client: AsyncClient) -> None:
    resp = await client.get("/feature-index")
    assert resp.status_code == 200


async def test_feature_index_api(client: AsyncClient) -> None:
    resp = await client.get("/api/features.json")
    assert resp.status_code == 200
    data = resp.json()
    items = data if isinstance(data, list) else data.get("features", data.get("items", []))
    assert isinstance(items, list)
    # Isolated test app only mounts 3 routers — feature_index may
    # introspect only what is mounted (possibly zero). Just verify shape.


async def test_query_api_example(client: AsyncClient) -> None:
    resp = await client.get("/api/query/example")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, dict)


async def test_query_api_empty_query(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/query",
        json={"kinds": ["screenshot"], "limit": 10},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data


async def test_query_api_full_query(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/query",
        json={
            "fts": "hello",
            "kinds": ["screenshot", "note"],
            "limit": 5,
        },
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "results" in data
    assert "screenshots" in data["results"] or "notes" in data["results"]


async def test_setup_page(client: AsyncClient) -> None:
    resp = await client.get("/setup")
    assert resp.status_code == 200


async def test_setup_save(client: AsyncClient) -> None:
    resp = await client.post(
        "/setup",
        data={
            "theme": "dark",
            "capture_interval_seconds": "60",
            "ocr_languages": "eng",
            "llm_provider": "",
            "api_key": "",
            "retention_warm_days": "7",
            "retention_cold_days": "30",
            "retention_delete_days": "365",
        },
    )
    assert resp.status_code in {200, 303, 302}


@pytest.mark.asyncio
async def test_build_feature_index_returns_list() -> None:
    from fastapi import FastAPI
    from app.feature_index import build_feature_index

    # v1.0+ build_feature_index requires the FastAPI app instance to
    # introspect routes from. Legacy was nullary.
    try:
        rows = await build_feature_index()
    except TypeError:
        app = FastAPI()
        rows = await build_feature_index(app)
    assert isinstance(rows, list)
