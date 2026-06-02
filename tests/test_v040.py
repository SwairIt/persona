"""Tests for v0.40 — SSE live status + per-day OCR .txt + undo bin."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import ocr_txt_export as ocr_txt_export_routes
    from app.web.routes import recycle as recycle_routes

    await init_database()
    app = FastAPI()
    app.include_router(ocr_txt_export_routes.router)
    app.include_router(recycle_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_ocr_txt_export_returns_text(client: AsyncClient) -> None:
    resp = await client.get("/export/ocr.txt?day=2026-06-02")
    assert resp.status_code == 200
    ctype = resp.headers.get("content-type", "")
    assert "text/plain" in ctype or "text/" in ctype


@pytest.mark.asyncio
async def test_export_day_ocr_txt_empty() -> None:
    await init_database()
    from app.ocr_txt_export import export_day_ocr_txt

    text = await export_day_ocr_txt("2099-01-01")
    assert isinstance(text, str)


async def test_recycle_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/recycle")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_recycle_list_bin_empty() -> None:
    await init_database()
    from app.recycle import list_bin

    rows = await list_bin()
    assert isinstance(rows, list)


@pytest.mark.asyncio
async def test_recycle_purge_expired_safe_on_empty() -> None:
    await init_database()
    from app.recycle import purge_expired

    count = await purge_expired(retention_days=7)
    assert count == 0 or isinstance(count, int)


@pytest.mark.asyncio
async def test_recycle_settings() -> None:
    from app.settings import get_settings

    settings = get_settings()
    assert hasattr(settings, "recycle_retention_days")
    assert 1 <= settings.recycle_retention_days <= 90


@pytest.mark.asyncio
async def test_live_sse_route_module_importable() -> None:
    from app.web.routes import live_sse

    assert hasattr(live_sse, "router")


@pytest.mark.asyncio
async def test_live_status_js_exists() -> None:
    from pathlib import Path

    js = Path("C:/www-Yaroslav/Persona/app/web/static/live_status.js")
    assert js.exists()
    content = js.read_text(encoding="utf-8")
    assert "EventSource" in content
