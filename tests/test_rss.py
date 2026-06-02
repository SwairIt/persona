"""Smoke test for the RSS feed."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import aiosqlite
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.settings import get_settings
from app.storage.db import init_database
from app.storage.notes import upsert_note
from app.storage.repository import insert_screenshot


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import rss as rss_routes

    await init_database()
    app = FastAPI()
    app.include_router(rss_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_journal_feed_renders(client: AsyncClient) -> None:
    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        sid = await insert_screenshot(
            conn,
            captured_at=datetime.now(timezone.utc),
            width=10,
            height=10,
            phash="rss00000000000000",
            app_name="VS Code",
            window_title="main.py",
        )
        await upsert_note(conn, sid, "important fix for the auth flow")

    resp = await client.get("/feeds/journal.rss")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/rss+xml")
    assert "Persona Journal" in resp.text
    assert "important fix" in resp.text
