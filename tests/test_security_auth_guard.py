"""Регресс-сторож безопасности (Ф1, 2026-06-24).

Критичные роуты должны БЛОКИРОВАТЬ неаутентифицированный запрос
(route-level ``current_user_required``). Тест ловит случайное снятие auth —
если кто-то уберёт защиту, роут вернёт 200 и тест упадёт.

Минимальное приложение БЕЗ AuthGateMiddleware — проверяем именно route-level
зависимость (defense-in-depth, не полагающуюся на fail-open gate).
"""

from __future__ import annotations

import sqlite3

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database
from app.web.routes import about as about_routes
from app.web.routes import agents_admin as agents_admin_routes
from app.web.routes import capture_api
from app.web.routes import diag_bundle as diag_bundle_routes
from app.web.routes import doctor as doctor_routes
from app.web.routes import full_export as full_export_routes
from app.web.routes import mic_toggle as mic_toggle_routes
from app.web.routes import multi_shot_zip as multi_shot_zip_routes
from app.web.routes import public_day as public_day_routes
from app.web.routes import settings_api as settings_api_routes
from app.web.routes import thumbnails as thumbnails_routes
from app.web.routes import webhooks_routes

# (router, method, path) — реальные пути (роутеры включаются без префикса).
_CASES = [
    (full_export_routes.router, "GET", "/api/export/full.zip"),
    (settings_api_routes.router, "GET", "/api/settings.json"),
    (capture_api.router, "POST", "/api/capture/pause"),
    (capture_api.router, "POST", "/api/capture/now"),
    (multi_shot_zip_routes.router, "POST", "/api/multi-shot-zip"),
    (diag_bundle_routes.router, "GET", "/admin/diagnostics-bundle.zip"),
    (doctor_routes.router, "GET", "/doctor"),
    (about_routes.router, "GET", "/about"),
    (agents_admin_routes.router, "GET", "/admin/agents"),
    (thumbnails_routes.router, "GET", "/thumbs/2026-06-23/1.webp"),
    (webhooks_routes.router, "POST", "/webhooks"),
    (public_day_routes.router, "POST", "/admin/public-days"),
    (mic_toggle_routes.router, "POST", "/api/audio/mic"),  # POST owner-only; GET остаётся публичным
]

# Заблокировано = редирект на логин ИЛИ 401/403 (НЕ 200, нет утечки данных).
_BLOCKED = {301, 302, 303, 307, 401, 403}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "router,method,path", _CASES, ids=[f"{m}_{p}" for _, m, p in _CASES]
)
async def test_protected_route_blocks_anonymous(router, method: str, path: str) -> None:
    await init_database()
    app = FastAPI()
    app.include_router(router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.request(method, path)
    assert resp.status_code in _BLOCKED, (
        f"{method} {path} -> {resp.status_code}: ОЖИДАЛАСЬ блокировка анонима "
        f"(current_user_required снят?)"
    )


@pytest.mark.asyncio
async def test_gate_fails_closed_when_user_lookup_raises(monkeypatch) -> None:
    """Сбой БД в ``_gate_active`` НЕ должен открывать приватные роуты.

    Раньше ``except`` ставил ``active = False`` («пользователей нет» → гейт
    выключен) и кэшировал это на 60 с: один транзиентный лок SQLite отдавал
    приватные страницы анониму на целую минуту. Инвариант теперь: «не смогли
    выяснить» ≠ «пользователей нет» — считаем гейт активным.
    """
    from app.web.middleware import auth_gate
    from app.web.middleware.auth_gate import AuthGateMiddleware

    await init_database()
    auth_gate._cache["value"] = False
    auth_gate._cache["checked_at"] = 0.0
    auth_gate._owner_exclusive_cache["value"] = False
    auth_gate._owner_exclusive_cache["checked_at"] = 0.0

    def _boom(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(auth_gate, "get_connection", _boom)

    app = FastAPI()
    app.add_middleware(AuthGateMiddleware)

    @app.get("/private")
    async def _private() -> PlainTextResponse:
        return PlainTextResponse("PRIVATE")

    try:
        assert await auth_gate._gate_active() is True
        # ошибка кэшируется как «активен», а не как «выключен»
        assert auth_gate._cache["value"] is True

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/private", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/landing"
    finally:
        auth_gate._cache["value"] = False
        auth_gate._cache["checked_at"] = 0.0
        auth_gate._owner_exclusive_cache["value"] = False
        auth_gate._owner_exclusive_cache["checked_at"] = 0.0
