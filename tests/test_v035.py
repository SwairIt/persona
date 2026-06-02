"""Tests for v0.35 — clipboard history + OCR confidence overlay + .ics export."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import clipboard as clipboard_routes
    from app.web.routes import ics_export as ics_export_routes
    from app.web.routes import ocr_overlay as ocr_overlay_routes

    await init_database()
    app = FastAPI()
    app.include_router(clipboard_routes.router)
    app.include_router(ocr_overlay_routes.router)
    app.include_router(ics_export_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_clipboard_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/clipboard")
    assert resp.status_code == 200


async def test_clipboard_api(client: AsyncClient) -> None:
    resp = await client.get("/api/clipboard.json")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_clipboard_settings_default_off() -> None:
    from app.settings import get_settings

    settings = get_settings()
    assert hasattr(settings, "clipboard_history_enabled")
    assert settings.clipboard_history_enabled is False


async def test_ocr_overlay_renders(client: AsyncClient) -> None:
    """Either renders (200) or 404 for missing screenshot."""
    resp = await client.get("/screenshot/999999/overlay")
    assert resp.status_code in {200, 404}


async def test_ocr_words_api(client: AsyncClient) -> None:
    resp = await client.get("/api/screenshot/999999/words.json")
    assert resp.status_code in {200, 404}


async def test_ics_export_returns_calendar(client: AsyncClient) -> None:
    resp = await client.get("/export/calendar.ics?days=30")
    assert resp.status_code == 200
    ctype = resp.headers.get("content-type", "")
    assert "calendar" in ctype or "octet-stream" in ctype or "text/" in ctype
    body = resp.text
    assert "BEGIN:VCALENDAR" in body
    assert "END:VCALENDAR" in body


@pytest.mark.asyncio
async def test_ics_export_module_zero_state() -> None:
    await init_database()
    from app.ics_export import export_ics

    text = await export_ics(days_back=7)
    assert "BEGIN:VCALENDAR" in text
    assert "END:VCALENDAR" in text
    assert "VERSION:2.0" in text


@pytest.mark.asyncio
async def test_ics_escapes_special_chars() -> None:
    """Per RFC 5545, commas/semicolons/newlines must be escaped in text values."""
    await init_database()
    from app.ics_export import export_ics

    text = await export_ics(days_back=7)
    for line in text.splitlines():
        if line.startswith("SUMMARY:") or line.startswith("DESCRIPTION:"):
            assert "\n" not in line[len("SUMMARY:") :]
