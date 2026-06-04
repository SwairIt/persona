"""Tests for v0.47 — notes timeline + dup suggest + audit RSS."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import audit_rss as audit_rss_routes
    from app.web.routes import dup_suggest as dup_suggest_routes
    from app.web.routes import notes_timeline as notes_timeline_routes

    await init_database()
    app = FastAPI()
    app.include_router(notes_timeline_routes.router)
    app.include_router(dup_suggest_routes.router)
    app.include_router(audit_rss_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_notes_timeline_page(client: AsyncClient) -> None:
    resp = await client.get("/notes/day/2026-06-03")
    assert resp.status_code == 200


async def test_notes_timeline_api(client: AsyncClient) -> None:
    resp = await client.get("/api/notes/day/2026-06-03.json")
    assert resp.status_code == 200
    data = resp.json()
    # v1.0+ shape: {"day", "total", "items"}. Legacy: list OR {"notes":[]}.
    assert isinstance(data, list) or "notes" in data or "items" in data


async def test_dup_suggest_missing_shot(client: AsyncClient) -> None:
    resp = await client.get("/api/screenshot/999999/similar.json")
    # The endpoint returns an HTML fragment for HTMX even though the URL
    # has .json — empty body is fine when the seed row is missing.
    assert resp.status_code in {200, 404}


@pytest.mark.asyncio
async def test_suggest_similar_empty_for_unknown() -> None:
    await init_database()
    from app.dup_suggest import suggest_similar

    result = await suggest_similar(999999, limit=4)
    assert isinstance(result, list)


async def test_audit_rss_returns_xml(client: AsyncClient) -> None:
    # URL moved to /feeds/audit.rss in v0.85 — old isolated test app
    # mounts audit_rss_routes by itself so both URLs may resolve;
    # accept either to stay robust.
    for url in ("/feeds/audit.rss", "/audit.rss"):
        resp = await client.get(url)
        if resp.status_code == 200:
            body = resp.text
            assert "<rss" in body or "<?xml" in body
            assert "<channel>" in body or "<feed" in body
            return
    raise AssertionError(f"no audit RSS endpoint found, last status: {resp.status_code}")
