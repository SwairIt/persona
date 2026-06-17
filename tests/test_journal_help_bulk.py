"""Smoke tests for /journal, /help and /api/bulk/* routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import aiosqlite
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database
from app.storage.notes import upsert_note
from app.storage.repository import insert_screenshot


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import bulk as bulk_routes
    from app.web.routes import help as help_routes
    from app.web.routes import journal as journal_routes

    await init_database()
    app = FastAPI()
    app.include_router(journal_routes.router)
    app.include_router(help_routes.router)
    app.include_router(bulk_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _seed_screenshot_with_note() -> int:
    from app.settings import get_settings

    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        sid = await insert_screenshot(
            conn,
            captured_at=datetime(2026, 6, 1, 14, 0, tzinfo=timezone.utc),
            width=10,
            height=10,
            phash="jjjjjjjjjjjjjjjj",
            app_name="VS Code",
            window_title="main.py",
        )
        await upsert_note(conn, sid, "**Important** moment with `code`")
    return sid


async def test_journal_empty(client: AsyncClient) -> None:
    resp = await client.get("/journal")
    assert resp.status_code == 200
    assert "empty" in resp.text.lower() or "0 moments" in resp.text


async def test_journal_with_entry(client: AsyncClient) -> None:
    await _seed_screenshot_with_note()
    resp = await client.get("/journal")
    assert resp.status_code == 200
    assert "VS Code" in resp.text


async def test_help_renders(client: AsyncClient) -> None:
    # T10 (2026-06-07): the keyboard-shortcuts cheatsheet moved from /help
    # (now the friendly walkthrough in help_walkthrough.py) to /help/shortcuts
    # in this router. Assert the legacy shortcuts page this router still serves.
    resp = await client.get("/help/shortcuts")
    assert resp.status_code == 200
    assert "Keyboard shortcuts" in resp.text
    assert "Hybrid" in resp.text


async def test_bulk_delete_by_app_requires_confirmation(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/bulk/delete-by-app",
        data={"app": "Discord", "confirm": "Slack"},
    )
    assert resp.status_code == 400


async def test_bulk_delete_by_app_removes_rows(client: AsyncClient) -> None:
    from app.settings import get_settings

    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        for i in range(3):
            await insert_screenshot(
                conn,
                captured_at=datetime(2026, 6, 1, 10, i, tzinfo=timezone.utc),
                width=10,
                height=10,
                phash=f"b{i:015d}",
                app_name="Discord",
            )

    resp = await client.post(
        "/api/bulk/delete-by-app",
        data={"app": "Discord", "confirm": "Discord"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["deleted_rows"] == 3


async def test_bulk_delete_range_requires_yes(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/bulk/delete-by-range",
        data={
            "since": "2026-01-01T00:00:00+00:00",
            "until": "2026-12-31T00:00:00+00:00",
            "confirm": "maybe",
        },
    )
    assert resp.status_code == 400
