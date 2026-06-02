"""Tests for v0.48 — permalinks + reading time + tag merge."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import permalinks as permalinks_routes
    from app.web.routes import reading_time as reading_time_routes
    from app.web.routes import tag_merge as tag_merge_routes

    await init_database()
    app = FastAPI()
    app.include_router(permalinks_routes.router)
    app.include_router(reading_time_routes.router)
    app.include_router(tag_merge_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_permalinks_page(client: AsyncClient) -> None:
    resp = await client.get("/permalinks")
    assert resp.status_code == 200


async def test_create_permalink_and_redirect(client: AsyncClient) -> None:
    create = await client.post(
        "/api/permalink",
        data={"target_url": "/search?q=test", "label": "test"},
    )
    assert create.status_code in {200, 201}
    body = create.json()
    slug = body.get("slug")
    assert slug is not None
    assert len(slug) >= 6

    follow = await client.get(f"/go/{slug}", follow_redirects=False)
    assert follow.status_code in {302, 303, 307}
    loc = follow.headers.get("location", "")
    assert "search" in loc.lower() or loc.startswith("/")


async def test_permalink_open_redirect_blocked(client: AsyncClient) -> None:
    """target_url must be relative (starts with /), no http://."""
    resp = await client.post(
        "/api/permalink",
        data={"target_url": "https://evil.example.com/", "label": "x"},
    )
    assert resp.status_code == 400


async def test_reading_time_page(client: AsyncClient) -> None:
    resp = await client.get("/stats/reading-time?day=2026-06-03")
    assert resp.status_code == 200


async def test_reading_time_api(client: AsyncClient) -> None:
    resp = await client.get("/api/reading-time.json?day=2026-06-03")
    assert resp.status_code == 200
    data = resp.json()
    assert "total_words_ocr" in data or "minutes_at_250wpm" in data


@pytest.mark.asyncio
async def test_reading_time_empty_day() -> None:
    await init_database()
    from app.reading_time import reading_time_for_day

    result = await reading_time_for_day("2099-01-01")
    assert result["total_words_ocr"] == 0
    assert result["total_words_notes"] == 0


async def test_tag_merge_page(client: AsyncClient) -> None:
    resp = await client.get("/admin/tag-merge")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_merge_tags_dry_run_safe() -> None:
    await init_database()
    from app.tag_merge import merge_tags

    result = await merge_tags("nonexistent-source", "nonexistent-dest", dry_run=True)
    assert result["dry_run"] is True
    assert result["moved"] == 0
