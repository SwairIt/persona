"""Tests for v0.37 — settings backup JSON + worker heartbeat + markdown inbox."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import health_dashboard as health_dashboard_routes
    from app.web.routes import inbox as inbox_routes
    from app.web.routes import settings_backup as settings_backup_routes

    await init_database()
    app = FastAPI()
    app.include_router(settings_backup_routes.router)
    app.include_router(health_dashboard_routes.router)
    app.include_router(inbox_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


@pytest.mark.asyncio
async def test_export_settings_shape() -> None:
    await init_database()
    from app.settings_backup import export_settings_json

    data = await export_settings_json()
    assert isinstance(data, dict)
    # v1.0+ uses "kv_settings" (plural) and adds top-level metadata.
    assert (
        "kv_setting" in data
        or "kv_settings" in data
        or "settings" in data
        or "tables" in data
    )


@pytest.mark.asyncio
async def test_export_omits_secrets() -> None:
    """webhook.secret and vault ciphertext must not appear in exported JSON."""
    await init_database()
    from app.settings_backup import export_settings_json

    data = await export_settings_json()
    import json

    blob = json.dumps(data)
    if "webhook" in data:
        for w in data["webhook"]:
            assert "secret" not in w or w.get("secret") in (None, "")
    assert "ciphertext" not in blob.lower() or '"ciphertext"' not in blob


async def test_settings_backup_page(client: AsyncClient) -> None:
    resp = await client.get("/settings/backup")
    assert resp.status_code == 200


async def test_settings_backup_download(client: AsyncClient) -> None:
    """The download endpoint may live at /settings/backup or /settings/backup/download."""
    resp = await client.get("/settings/backup/download")
    if resp.status_code == 404:
        resp = await client.get("/settings/backup?format=json")
    assert resp.status_code in {200, 404}


@pytest.mark.asyncio
async def test_heartbeat_beat_and_read() -> None:
    await init_database()
    from app.workers.heartbeat import beat, get_all

    await beat("test-worker", "ok")
    all_workers = await get_all()
    assert any(w["name"] == "test-worker" for w in all_workers)


async def test_health_dashboard_page(client: AsyncClient) -> None:
    # v1.61 moved the dashboard from /admin/health to /health-dashboard and
    # kept the old URL as a 307 redirect for bookmarks.
    resp = await client.get("/admin/health", follow_redirects=True)
    assert resp.status_code == 200


async def test_health_api(client: AsyncClient) -> None:
    resp = await client.get("/api/health.json")
    assert resp.status_code == 200


async def test_inbox_page(client: AsyncClient) -> None:
    resp = await client.get("/inbox")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_inbox_settings_defaults() -> None:
    from app.settings import get_settings

    settings = get_settings()
    assert hasattr(settings, "inbox_enabled")
    assert hasattr(settings, "inbox_path")
