"""Tests for v0.26 — lock-aware pause + power-aware capture + notes FTS search."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import notes_search as notes_search_routes

    await init_database()
    app = FastAPI()
    app.include_router(notes_search_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


@pytest.mark.asyncio
async def test_session_state_module_importable() -> None:
    from app.capture import session_state

    assert hasattr(session_state, "is_session_locked")


@pytest.mark.asyncio
async def test_is_session_locked_returns_bool() -> None:
    from app.capture.session_state import is_session_locked

    result = await is_session_locked()
    assert isinstance(result, bool)


@pytest.mark.asyncio
async def test_power_state_module_importable() -> None:
    from app.capture import power_state

    assert hasattr(power_state, "get_power_state")


@pytest.mark.asyncio
async def test_get_power_state_shape() -> None:
    from app.capture.power_state import get_power_state_async

    state = await get_power_state_async()
    assert "on_battery" in state
    assert "percent" in state
    assert "plugged" in state
    assert isinstance(state["on_battery"], bool)


async def test_notes_search_empty_query_renders(client: AsyncClient) -> None:
    # v1.32 — page moved into /search/everything; standalone URL now
    # 301-redirects. Accept either the redirect or the legacy 200 so
    # the test remains valid across the deprecation window.
    resp = await client.get("/notes/search", follow_redirects=False)
    assert resp.status_code in {200, 301}


async def test_notes_search_with_query_renders(client: AsyncClient) -> None:
    resp = await client.get("/notes/search?q=hello", follow_redirects=False)
    assert resp.status_code in {200, 301}
    if resp.status_code == 301:
        assert "/search/everything" in resp.headers.get("location", "")


async def test_notes_search_api_returns_json(client: AsyncClient) -> None:
    resp = await client.get("/api/notes/search.json?q=hello")
    assert resp.status_code == 200
    data = resp.json()
    assert "query" in data
    assert "results" in data
    assert "total" in data
    assert data["query"] == "hello"
    assert isinstance(data["results"], list)


async def test_notes_search_api_empty_query(client: AsyncClient) -> None:
    resp = await client.get("/api/notes/search.json?q=")
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"] == []


@pytest.mark.asyncio
async def test_battery_settings_defaults() -> None:
    from app.settings import get_settings

    settings = get_settings()
    assert hasattr(settings, "battery_aware_enabled")
    assert hasattr(settings, "battery_capture_multiplier")
    assert hasattr(settings, "battery_critical_pct")
    assert hasattr(settings, "lock_aware_pause_enabled")
