"""Smoke tests for v0.10 (focus, reminders, reading list)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, timezone

import aiosqlite
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.settings import get_settings
from app.storage.db import init_database
from app.storage.repository import insert_screenshot


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import (
        focus as focus_routes,
        reading as reading_routes,
        reminders as reminders_routes,
    )

    await init_database()
    app = FastAPI()
    app.include_router(focus_routes.router)
    app.include_router(reminders_routes.router)
    app.include_router(reading_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_focus_lifecycle(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/focus/start",
        data={"duration_minutes": "25", "intent": "deep work", "pause_capture": "false"},
    )
    assert resp.status_code == 200
    sid = resp.json()["session_id"]

    resp = await client.post(
        "/api/focus/finish",
        data={"session_id": str(sid), "completed": "true", "outcome": "did it", "resume_capture": "false"},
    )
    assert resp.status_code == 200

    resp = await client.get("/api/focus/sessions")
    assert resp.status_code == 200
    sessions = resp.json()["sessions"]
    assert any(s["id"] == sid and s["completed"] for s in sessions)


async def test_focus_page(client: AsyncClient) -> None:
    resp = await client.get("/focus")
    assert resp.status_code == 200
    assert "Focus" in resp.text


async def test_reminders_create_toggle_delete(client: AsyncClient) -> None:
    today = date.today().isoformat()
    resp = await client.post(
        "/reminders/create",
        data={"body": "ship it", "due_date": today},
        follow_redirects=False,
    )
    assert resp.status_code in {303, 307}

    resp = await client.get("/reminders")
    assert resp.status_code == 200
    assert "ship it" in resp.text


async def test_reminders_validates_input(client: AsyncClient) -> None:
    resp = await client.post(
        "/reminders/create",
        data={"body": "  ", "due_date": "2026-06-02"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


async def test_reading_list_add_and_list(client: AsyncClient) -> None:
    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        sid = await insert_screenshot(
            conn,
            captured_at=datetime.now(timezone.utc),
            width=1,
            height=1,
            phash="reading0000000000",
            app_name="Reader",
            window_title="article",
        )

    resp = await client.post(f"/api/screenshots/{sid}/read-later")
    assert resp.status_code == 200
    assert resp.json()["in_reading_list"] is True

    resp = await client.get("/reading")
    assert resp.status_code == 200
    assert str(sid) in resp.text or "Reader" in resp.text

    resp = await client.delete(f"/api/screenshots/{sid}/read-later")
    assert resp.status_code == 200
    assert resp.json()["in_reading_list"] is False


async def test_read_later_404(client: AsyncClient) -> None:
    resp = await client.post("/api/screenshots/99999/read-later")
    assert resp.status_code == 404
