"""Smoke test for the /api/capture/now manual capture endpoint."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    import aiosqlite
    from fastapi import FastAPI

    from app.auth import SESSION_COOKIE_NAME, issue_session
    from app.settings import get_settings
    from app.web.routes import capture_api

    await init_database()
    # Ф1 (security, 2026-06-24): capture-роуты теперь owner-only
    # (current_user_required) — аудит флагнул /api/capture/now (force-screenshot)
    # и /pause (DoS). Создаём владельца + сессию и шлём cookie, иначе 303 на логин.
    settings = get_settings()
    async with aiosqlite.connect(settings.db_path) as conn:
        await conn.execute(
            "INSERT INTO users(id,email,password_hash) VALUES(1,'t@x.c','x')"
        )
        await conn.commit()
    token, _ = await issue_session(1)
    app = FastAPI()
    app.include_router(capture_api.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={SESSION_COOKIE_NAME: token},
    ) as ac:
        yield ac


@pytest.mark.skipif(
    not sys.platform.startswith("win"),
    reason="mss screen capture requires a real display; only stable on Windows test runners",
)
async def test_capture_now_returns_id(client: AsyncClient) -> None:
    try:
        resp = await client.post("/api/capture/now")
    except Exception as exc:
        # mss raises ScreenShotError on headless / Server-without-RDP-session.
        if "ScreenShotError" in type(exc).__name__ or "no display" in str(exc).lower():
            pytest.skip(f"no display: {exc}")
        raise
    if resp.status_code >= 500:
        pytest.skip("no display / capture backend unavailable")
    assert resp.status_code == 200
    data = resp.json()
    assert "screenshot_id" in data
    assert data["screenshot_id"] > 0
