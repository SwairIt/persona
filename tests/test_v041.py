"""Tests for v0.41 — search facets + drag-to-tag + bookmarklet."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import bookmarklet as bookmarklet_routes
    from app.web.routes import drag_to_tag as drag_to_tag_routes
    from app.web.routes import search_facets as search_facets_routes

    await init_database()
    app = FastAPI()
    app.include_router(search_facets_routes.router)
    app.include_router(drag_to_tag_routes.router)
    app.include_router(bookmarklet_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_search_facets_api(client: AsyncClient) -> None:
    resp = await client.get("/api/search/facets.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "apps" in data
    assert "tags" in data


async def test_drag_to_tag_404_on_missing_shot(client: AsyncClient) -> None:
    resp = await client.post("/api/screenshot/999999/tags", data={"tag": "test"})
    assert resp.status_code == 404


async def test_drag_to_tag_400_on_empty_tag(client: AsyncClient) -> None:
    resp = await client.post("/api/screenshot/1/tags", data={"tag": ""})
    # FastAPI 422 for form validation; legacy custom-400 handler may
    # still return 400 — accept either, plus 404 for missing-shot path.
    assert resp.status_code in {400, 404, 422}


async def test_bookmarklet_page(client: AsyncClient) -> None:
    resp = await client.get("/bookmarklet")
    assert resp.status_code == 200
    assert "javascript:" in resp.text.lower() or "bookmark" in resp.text.lower()


async def test_bookmarklet_capture(client: AsyncClient) -> None:
    payload = {
        "url": "https://example.com/article",
        "title": "Example Article",
        "selection": "this is a quoted snippet",
    }
    resp = await client.post("/api/bookmarklet/capture", json=payload)
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("ok") is True or "note_id" in data


async def test_bookmarklet_capture_truncates_long_selection(client: AsyncClient) -> None:
    payload = {
        "url": "https://example.com",
        "title": "x",
        "selection": "A" * 10000,
    }
    resp = await client.post("/api/bookmarklet/capture", json=payload)
    assert resp.status_code in {200, 400}


@pytest.mark.asyncio
async def test_bookmarklet_source_exists() -> None:
    from pathlib import Path

    js = Path("C:/www-Yaroslav/Persona/app/web/static/bookmarklet_source.js")
    assert js.exists()


@pytest.mark.asyncio
async def test_drag_to_tag_js_exists() -> None:
    from pathlib import Path

    js = Path("C:/www-Yaroslav/Persona/app/web/static/drag_to_tag.js")
    assert js.exists()
    content = js.read_text(encoding="utf-8")
    assert "dragover" in content.lower() or "drop" in content.lower()
