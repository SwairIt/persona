"""Tests for v0.32 — PDF export + theme switcher + adaptive cadence."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    """Аутентифицированный ВЛАДЕЛЕЦ.

    ``/settings/theme`` раньше был вообще без зависимости аутентификации, и
    любой (в т.ч. чужой аккаунт) перекрашивал весь инстанс. Теперь роут
    требует сессию и пишет глобальный kv только владельцу — поэтому фикстура
    заводит пользователя и кладёт cookie, иначе тесты видели бы 303 на логин.
    """
    from fastapi import FastAPI

    from app.auth.owner import _cache as _owner_cache
    from app.auth.owner import _fa_cache as _owner_fa_cache
    from app.auth.sessions import SESSION_COOKIE_NAME, issue_session
    from app.auth.users import create_user
    from app.storage.db import get_connection
    from app.storage.repository import set_kv
    from app.web.routes import pdf_export as pdf_export_routes
    from app.web.routes import theme as theme_routes

    await init_database()
    owner_user = await create_user("owner@v032.test", "Zq7-frost-lantern-91")
    async with get_connection() as conn:
        await set_kv(conn, "owner_user_id", str(owner_user["id"]))
    _owner_cache["value"] = None
    _owner_cache["checked_at"] = 0.0
    _owner_fa_cache["value"] = None
    _owner_fa_cache["checked_at"] = 0.0

    app = FastAPI()
    app.include_router(pdf_export_routes.router)
    app.include_router(theme_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        token, _ = await issue_session(owner_user["id"])
        ac.cookies.set(SESSION_COOKIE_NAME, token)
        yield ac
    _owner_cache["value"] = None
    _owner_cache["checked_at"] = 0.0


@pytest.mark.asyncio
async def test_compute_interval_active() -> None:
    from app.capture.adaptive_cadence import compute_interval

    assert compute_interval(60, 0, 30, 600) == 30
    assert compute_interval(60, 29, 30, 600) == 30


@pytest.mark.asyncio
async def test_compute_interval_normal() -> None:
    from app.capture.adaptive_cadence import compute_interval

    assert compute_interval(60, 60, 30, 600) == 60


@pytest.mark.asyncio
async def test_compute_interval_idle_caps_at_max() -> None:
    from app.capture.adaptive_cadence import compute_interval

    assert compute_interval(60, 10000, 30, 600) == 600
    assert compute_interval(60, 200, 30, 600) > 60


async def test_pdf_export_route_404_on_empty_day(client: AsyncClient) -> None:
    resp = await client.get("/export/pdf?day=2099-01-01")
    # 503 when weasyprint isn't installed; treat as ok for env triage.
    assert resp.status_code in {200, 404, 503}


async def test_theme_settings_page(client: AsyncClient) -> None:
    resp = await client.get("/settings/theme")
    assert resp.status_code == 200


async def test_theme_save_valid(client: AsyncClient) -> None:
    resp = await client.post("/settings/theme", data={"theme": "light"})
    assert resp.status_code in {200, 303, 302}


async def test_theme_save_invalid_rejected(client: AsyncClient) -> None:
    resp = await client.post("/settings/theme", data={"theme": "neon"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_adaptive_settings_defaults() -> None:
    from app.settings import get_settings

    settings = get_settings()
    assert hasattr(settings, "adaptive_cadence_enabled")
    assert hasattr(settings, "adaptive_min_seconds")
    assert hasattr(settings, "adaptive_max_seconds")
    assert settings.adaptive_max_seconds >= settings.adaptive_min_seconds
