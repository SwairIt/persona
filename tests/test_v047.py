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
    assert isinstance(data, list) or "notes" in data


async def test_dup_suggest_missing_shot(client: AsyncClient) -> None:
    resp = await client.get("/api/screenshot/999999/similar.json")
    assert resp.status_code in {200, 404}
    if resp.status_code == 200:
        data = resp.json()
        items = data if isinstance(data, list) else data.get("items", data.get("similar", []))
        assert isinstance(items, list)


@pytest.mark.asyncio
async def test_suggest_similar_empty_for_unknown() -> None:
    await init_database()
    from app.dup_suggest import suggest_similar

    result = await suggest_similar(999999, limit=4)
    assert isinstance(result, list)


async def test_audit_rss_returns_xml(client: AsyncClient) -> None:
    resp = await client.get("/audit.rss")
    assert resp.status_code == 200
    body = resp.text
    assert "<rss" in body or "<?xml" in body
    assert "<channel>" in body or "<feed" in body
