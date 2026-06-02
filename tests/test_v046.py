"""Tests for v0.46 — tag colour + image viewer + day kanban."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import day_kanban as day_kanban_routes
    from app.web.routes import tag_colour as tag_colour_routes

    await init_database()
    app = FastAPI()
    app.include_router(tag_colour_routes.router)
    app.include_router(day_kanban_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_tag_colour_valid_hex(client: AsyncClient) -> None:
    resp = await client.post("/api/tags/work/color", data={"color": "#ec4899"})
    assert resp.status_code in {200, 303, 302}


async def test_tag_colour_invalid_hex_rejected(client: AsyncClient) -> None:
    resp = await client.post("/api/tags/work/color", data={"color": "not-a-hex"})
    assert resp.status_code == 400


async def test_tag_colour_short_hex_rejected(client: AsyncClient) -> None:
    resp = await client.post("/api/tags/work/color", data={"color": "#fff"})
    assert resp.status_code == 400


async def test_day_kanban_page(client: AsyncClient) -> None:
    resp = await client.get("/kanban/2026-06-03")
    assert resp.status_code == 200


async def test_day_kanban_api(client: AsyncClient) -> None:
    resp = await client.get("/api/kanban/2026-06-03.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "columns" in data
    assert isinstance(data["columns"], list)


@pytest.mark.asyncio
async def test_image_viewer_static_files() -> None:
    from pathlib import Path

    js = Path("C:/www-Yaroslav/Persona/app/web/static/image_viewer.js")
    css = Path("C:/www-Yaroslav/Persona/app/web/static/image_viewer.css")
    assert js.exists()
    assert css.exists()
    js_content = js.read_text(encoding="utf-8")
    assert "data-zoomable" in js_content
    assert "transform" in js_content.lower()
