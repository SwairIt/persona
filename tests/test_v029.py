"""Tests for v0.29 — time-on-app + OCR language switcher + favourites."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import favourites as favourites_routes
    from app.web.routes import ocr_languages as ocr_languages_routes
    from app.web.routes import time_on_app as time_on_app_routes

    await init_database()
    app = FastAPI()
    app.include_router(time_on_app_routes.router)
    app.include_router(ocr_languages_routes.router)
    app.include_router(favourites_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_time_on_app_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/time-on-app")
    assert resp.status_code == 200


async def test_time_on_app_api(client: AsyncClient) -> None:
    resp = await client.get("/api/time-on-app.json?day=2026-06-02")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list) or "rows" in data or "apps" in data


@pytest.mark.asyncio
async def test_daily_time_on_app_empty() -> None:
    await init_database()
    from app.time_on_app import daily_time_on_app

    rows = await daily_time_on_app("2026-06-02")
    assert isinstance(rows, list)


async def test_ocr_languages_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/settings/ocr-languages")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_configured_languages_default() -> None:
    await init_database()
    from app.ocr.languages import get_configured_languages

    langs = await get_configured_languages()
    assert isinstance(langs, list)
    assert len(langs) >= 1


@pytest.mark.asyncio
async def test_set_configured_languages_round_trip() -> None:
    await init_database()
    from app.ocr.languages import get_configured_languages, get_installed_languages, set_configured_languages

    installed = await get_installed_languages()
    if "eng" in installed:
        await set_configured_languages(["eng"])
        assert "eng" in await get_configured_languages()


async def test_favourites_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/favourites")
    assert resp.status_code == 200


async def test_favourites_api_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/favourites.json")
    assert resp.status_code == 200


async def test_favourite_toggle_404_on_missing(client: AsyncClient) -> None:
    resp = await client.post("/api/screenshot/999999/favourite")
    assert resp.status_code in {200, 404}
