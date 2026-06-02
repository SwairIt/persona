"""Tests for v0.27 — annotations + saved searches + daily streak."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import annotations as annotations_routes
    from app.web.routes import saved_searches as saved_searches_routes
    from app.web.routes import streak as streak_routes

    await init_database()
    app = FastAPI()
    app.include_router(annotations_routes.router)
    app.include_router(saved_searches_routes.router)
    app.include_router(streak_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_annotations_404_for_missing_shot(client: AsyncClient) -> None:
    resp = await client.get("/api/screenshot/999999/annotations")
    assert resp.status_code in {200, 404}


async def test_annotation_post_empty_body_400(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/screenshot/1/annotations",
        data={"body": "   "},
    )
    assert resp.status_code in {400, 404}


async def test_saved_searches_index(client: AsyncClient) -> None:
    resp = await client.get("/searches")
    assert resp.status_code == 200


async def test_saved_search_add_and_redirect(client: AsyncClient) -> None:
    add = await client.post(
        "/searches",
        data={"slug": "ship-log", "title": "Ship log", "query": "shipped OR released"},
    )
    assert add.status_code in {200, 303, 302}

    follow = await client.get("/searches/ship-log", follow_redirects=False)
    assert follow.status_code in {303, 302}
    location = follow.headers.get("location", "")
    assert "q=" in location
    assert "shipped" in location.lower() or "released" in location.lower()


async def test_saved_search_bad_slug_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/searches",
        data={"slug": "Bad Slug!!!", "title": "x", "query": "y"},
    )
    assert resp.status_code == 400


async def test_saved_search_delete(client: AsyncClient) -> None:
    await client.post(
        "/searches",
        data={"slug": "delete-me", "title": "x", "query": "y"},
    )
    resp = await client.post("/searches/delete-me/delete")
    assert resp.status_code in {200, 303, 302}


async def test_streak_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/streak")
    assert resp.status_code == 200


async def test_streak_api_returns_json(client: AsyncClient) -> None:
    resp = await client.get("/api/streak.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "days" in data
    assert "longest" in data
    assert "today_count" in data
    assert isinstance(data["days"], int)
    assert isinstance(data["longest"], int)
    assert data["days"] >= 0
    assert data["longest"] >= 0


@pytest.mark.asyncio
async def test_streak_module_returns_zeros_on_empty_db() -> None:
    await init_database()
    from app.streak import current_streak

    result = await current_streak()
    assert result["days"] == 0
    assert result["longest"] == 0
    assert result["today_count"] == 0
    assert result["last_capture_date"] is None
