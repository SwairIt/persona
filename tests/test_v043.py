"""Tests for v0.43 — query help + context menu + per-shot share link."""

from __future__ import annotations

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
    from app.web.routes import shot_share as shot_share_routes
    from app.web.routes import shot_share_ui as shot_share_ui_routes

    await init_database()
    # Ф (security, 2026-06-24): per-shot share create — owner-only.
    # Создаём владельца + сессию и шлём cookie, иначе 303 на логин.
    async with aiosqlite.connect(get_settings().db_path) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(1,'t@x.c','x')"
        )
        await conn.commit()
    token, _ = await issue_session(1)
    app = FastAPI()
    app.include_router(shot_share_routes.router)
    app.include_router(shot_share_ui_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://127.0.0.1",
        cookies={SESSION_COOKIE_NAME: token},
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_query_help_js_exists() -> None:
    from pathlib import Path

    js = Path("C:/www-Yaroslav/Persona/app/web/static/query_help.js")
    assert js.exists()
    content = js.read_text(encoding="utf-8")
    assert "query-help" in content.lower() or "popover" in content.lower()


@pytest.mark.asyncio
async def test_context_menu_js_exists() -> None:
    from pathlib import Path

    js = Path("C:/www-Yaroslav/Persona/app/web/static/context_menu.js")
    assert js.exists()
    content = js.read_text(encoding="utf-8")
    assert "contextmenu" in content.lower()


async def test_shot_share_create_404_on_missing(client: AsyncClient) -> None:
    resp = await client.post("/api/screenshot/999999/share/create", data={"ttl_hours": "24"})
    assert resp.status_code in {404, 400, 422}


async def test_shot_share_invalid_token_gone(client: AsyncClient) -> None:
    resp = await client.get("/shot/share/1/totally-fake-token-1234567890")
    assert resp.status_code in {410, 404}


async def test_shot_share_ui_renders_on_unknown(client: AsyncClient) -> None:
    resp = await client.get("/screenshot/1/share")
    assert resp.status_code in {200, 404}
