"""Tests for v0.39 — keyboard cheatsheet + OCR language stats + archive ZIP."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import archive_bundle as archive_bundle_routes
    from app.web.routes import ocr_language_stats as ocr_language_stats_routes

    await init_database()
    app = FastAPI()
    app.include_router(ocr_language_stats_routes.router)
    app.include_router(archive_bundle_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_ocr_lang_stats_page(client: AsyncClient) -> None:
    resp = await client.get("/stats/ocr-languages")
    assert resp.status_code == 200


async def test_ocr_lang_stats_api(client: AsyncClient) -> None:
    resp = await client.get("/api/ocr-languages.json")
    assert resp.status_code == 200
    data = resp.json()
    assert "cyrillic_chars" in data or "latin_chars" in data


@pytest.mark.asyncio
async def test_language_breakdown_returns_shape() -> None:
    await init_database()
    from app.ocr.language_stats import language_breakdown

    result = await language_breakdown(days=7)
    for key in ("cyrillic_chars", "latin_chars", "digit_chars", "total_chars"):
        assert key in result
    assert result["total_chars"] >= 0
    assert isinstance(result["total_chars"], int)


@pytest.mark.asyncio
async def test_language_breakdown_zero_state() -> None:
    await init_database()
    from app.ocr.language_stats import language_breakdown

    result = await language_breakdown(days=7)
    assert result["total_chars"] == 0


async def test_archive_bundle_endpoint(client: AsyncClient) -> None:
    """Archive endpoint should respond with zip or some status."""
    resp = await client.get("/export/archive.zip?days=1&thumbs=0")
    assert resp.status_code in {200, 503}
    if resp.status_code == 200:
        assert resp.content.startswith(b"PK") or "zip" in resp.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_build_archive_module(tmp_path) -> None:
    await init_database()
    from app.archive_bundle import build_archive

    out = tmp_path / "test.zip"
    result = await build_archive(days=1, output_path=out, include_thumbnails=False)
    assert result["status"] in {"ok", "empty"}
    if result["status"] == "ok":
        assert out.exists()
        assert result["size_bytes"] > 0


@pytest.mark.asyncio
async def test_keyboard_shortcuts_static_exists() -> None:
    from pathlib import Path

    js = Path("C:/www-Yaroslav/Persona/app/web/static/keyboard_shortcuts.js")
    assert js.exists()
    content = js.read_text(encoding="utf-8")
    assert "keydown" in content.lower()
