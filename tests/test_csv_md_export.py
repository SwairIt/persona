"""Tests for CSV and Markdown search exports."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import aiosqlite
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database
from app.storage.repository import insert_screenshot


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import csv_export

    await init_database()
    app = FastAPI()
    app.include_router(csv_export.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _seed() -> None:
    from app.settings import get_settings

    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        await insert_screenshot(
            conn,
            captured_at=datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc),
            width=10,
            height=10,
            phash="dddd",
            app_name="VS Code",
            window_title="main.py — persona",
        )


async def test_csv_export_includes_header(client: AsyncClient) -> None:
    await _seed()
    resp = await client.get("/api/export/search.csv?q=persona")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "captured_at" in resp.text
    assert "main.py" in resp.text


async def test_md_export_renders_header(client: AsyncClient) -> None:
    await _seed()
    resp = await client.get("/api/export/search.md?q=persona")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "# Persona" in resp.text


async def test_md_export_no_matches(client: AsyncClient) -> None:
    resp = await client.get("/api/export/search.md?q=zzzz_no_match_xxx")
    assert resp.status_code == 200
    assert "No matches" in resp.text
