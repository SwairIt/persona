"""Tests for v0.16 — neighbour lookup + bulk tag apply."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.settings import get_settings
from app.storage.db import init_database
from app.storage.repository import get_neighbour_ids, insert_screenshot


@pytest.mark.asyncio
async def test_neighbour_ids(db: aiosqlite.Connection) -> None:
    base = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
    ids = [
        await insert_screenshot(
            db,
            captured_at=base + timedelta(minutes=i),
            width=1,
            height=1,
            phash=f"nbr{i:013d}",
        )
        for i in range(3)
    ]
    prev_id, next_id = await get_neighbour_ids(db, screenshot_id=ids[1])
    assert prev_id == ids[0]
    assert next_id == ids[2]

    prev_id, next_id = await get_neighbour_ids(db, screenshot_id=ids[0])
    assert prev_id is None
    assert next_id == ids[1]

    prev_id, next_id = await get_neighbour_ids(db, screenshot_id=ids[-1])
    assert prev_id == ids[1]
    assert next_id is None


@pytest.mark.asyncio
async def test_neighbour_missing(db: aiosqlite.Connection) -> None:
    assert await get_neighbour_ids(db, screenshot_id=99999) == (None, None)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import tags as tags_routes

    await init_database()
    app = FastAPI()
    app.include_router(tags_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_bulk_apply_creates_tag_and_binds(client: AsyncClient) -> None:
    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        ids = [
            await insert_screenshot(
                conn,
                captured_at=datetime.now(timezone.utc),
                width=1,
                height=1,
                phash=f"bulkx{i:012d}",
            )
            for i in range(3)
        ]

    resp = await client.post(
        "/api/tags/bulk-apply",
        data={"tag_name": "Important!", "screenshot_ids": ",".join(str(i) for i in ids)},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["applied"] == 3
    assert data["tag"] == "important!"


async def test_bulk_apply_validates(client: AsyncClient) -> None:
    resp = await client.post("/api/tags/bulk-apply", data={"tag_name": "", "screenshot_ids": "1,2"})
    assert resp.status_code == 400

    resp = await client.post("/api/tags/bulk-apply", data={"tag_name": "ok", "screenshot_ids": "  "})
    assert resp.status_code == 400

    resp = await client.post("/api/tags/bulk-apply", data={"tag_name": "ok", "screenshot_ids": "abc"})
    assert resp.status_code == 400
