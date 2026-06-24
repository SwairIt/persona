"""Smoke tests for v0.15 (journal export, /about, bulk tag lookup)."""

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
from app.storage.tags import create_tag, get_tags_for_many, tag_screenshot


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.auth import SESSION_COOKIE_NAME, issue_session
    from app.web.routes import about as about_routes
    from app.web.routes import journal_export as journal_export_routes

    await init_database()
    # Ф (security, 2026-06-24): /about + journal export — owner-only.
    # Создаём владельца + сессию и шлём cookie, иначе 303 на логин.
    async with aiosqlite.connect(get_settings().db_path) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(1,'t@x.c','x')"
        )
        await conn.commit()
    token, _ = await issue_session(1)
    app = FastAPI()
    app.include_router(about_routes.router)
    app.include_router(journal_export_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={SESSION_COOKIE_NAME: token},
    ) as ac:
        yield ac


async def test_about_renders(client: AsyncClient) -> None:
    resp = await client.get("/about")
    assert resp.status_code == 200
    assert "Persona" in resp.text
    assert "Features" in resp.text


async def test_journal_export_404_when_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/export/journal.md?date=2099-01-01")
    assert resp.status_code == 404


async def test_journal_export_with_note(client: AsyncClient) -> None:
    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        sid = await insert_screenshot(
            conn,
            captured_at=datetime(2026, 6, 2, 14, 0, tzinfo=timezone.utc),
            width=1,
            height=1,
            phash="jx000000000001",
            app_name="VS Code",
            window_title="main.py",
        )
        await upsert_note(conn, sid, "**important** fix to the auth flow")

    resp = await client.get("/api/export/journal.md?date=2026-06-02")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "auth flow" in resp.text
    assert "VS Code" in resp.text


async def test_journal_export_invalid_date(client: AsyncClient) -> None:
    resp = await client.get("/api/export/journal.md?date=not-a-date")
    assert resp.status_code == 400


async def test_get_tags_for_many() -> None:
    await init_database()
    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        sid1 = await insert_screenshot(
            conn,
            captured_at=datetime.now(timezone.utc),
            width=1,
            height=1,
            phash="bulk000000000001",
        )
        sid2 = await insert_screenshot(
            conn,
            captured_at=datetime.now(timezone.utc),
            width=1,
            height=1,
            phash="bulk000000000002",
        )
        tid_a = await create_tag(conn, name="alpha")
        tid_b = await create_tag(conn, name="beta")
        await tag_screenshot(conn, sid1, tid_a)
        await tag_screenshot(conn, sid1, tid_b)
        await tag_screenshot(conn, sid2, tid_b)

        result = await get_tags_for_many(conn, [sid1, sid2])

    assert {t["name"] for t in result[sid1]} == {"alpha", "beta"}
    assert [t["name"] for t in result[sid2]] == ["beta"]
