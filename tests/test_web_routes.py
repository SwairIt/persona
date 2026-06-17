"""Smoke tests for FastAPI routes — no real capture loop is started here."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    # Build a thin app without lifespan (which would start background workers).
    from fastapi import FastAPI
    from fastapi.staticfiles import StaticFiles

    from app.web.main import STATIC_DIR
    from app.web.routes import (
        capture_api,
        landing as landing_routes,
        screenshot,
        search as search_routes,
        settings as settings_routes,
        stats,
        thumbnails as thumbnails_routes,
        timeline,
    )

    await init_database()

    app = FastAPI(title="Persona-Test")
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(timeline.router)
    app.include_router(landing_routes.router)  # owns "/" (home) since the landing reorg
    app.include_router(search_routes.router)
    app.include_router(screenshot.router)
    app.include_router(settings_routes.router)
    app.include_router(stats.router)
    app.include_router(capture_api.router)
    app.include_router(thumbnails_routes.router)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_home_renders(client: AsyncClient) -> None:
    # Home now 303-redirects to /setup on first boot. The isolated test
    # app may not mount /setup at all, in which case the redirect leads
    # to a 404. Accept either: the timeline router IS mounted, so a 303
    # or a 200 both prove the route works.
    resp = await client.get("/", follow_redirects=False)
    assert resp.status_code in {200, 303}


async def test_search_empty(client: AsyncClient) -> None:
    resp = await client.get("/search")
    assert resp.status_code == 200
    assert "Search" in resp.text


async def test_stats_renders(client: AsyncClient) -> None:
    resp = await client.get("/stats")
    assert resp.status_code == 200
    assert "Captures total" in resp.text


async def test_stats_json(client: AsyncClient) -> None:
    resp = await client.get("/stats.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "captures_total" in data
    assert "events_by_day" in data
    assert "top_apps" in data


async def test_settings_renders(client: AsyncClient) -> None:
    resp = await client.get("/settings")
    assert resp.status_code == 200
    assert "Settings" in resp.text


async def test_capture_status(client: AsyncClient) -> None:
    resp = await client.get("/api/capture/status")
    assert resp.status_code == 200
    data = resp.json()
    assert "paused" in data
    assert "captures_total" in data


async def test_capture_pause_and_start(client: AsyncClient) -> None:
    pause = await client.post("/api/capture/pause")
    assert pause.status_code == 200
    assert pause.json()["paused"] is True

    start = await client.post("/api/capture/start")
    assert start.status_code == 200
    assert start.json()["paused"] is False


async def test_screenshot_404(client: AsyncClient) -> None:
    resp = await client.get("/screenshot/99999")
    assert resp.status_code == 404
