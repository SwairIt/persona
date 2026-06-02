"""Tests for v0.23 — backup CLI + auto-collections + OCR skip-list."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import auto_collections as auto_collections_routes
    from app.web.routes import ocr_skip as ocr_skip_routes

    await init_database()
    app = FastAPI()
    app.include_router(auto_collections_routes.router)
    app.include_router(ocr_skip_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_collections_index_renders(client: AsyncClient) -> None:
    resp = await client.get("/collections")
    assert resp.status_code == 200


async def test_create_collection_rule_and_view(client: AsyncClient) -> None:
    resp = await client.post(
        "/collections",
        data={"slug": "ship-log", "title": "Ship log", "tag": "ship", "public": "1"},
    )
    assert resp.status_code in {200, 303, 302}

    resp = await client.get("/collection/ship-log")
    assert resp.status_code == 200


async def test_unknown_collection_404(client: AsyncClient) -> None:
    resp = await client.get("/collection/does-not-exist-xyz")
    assert resp.status_code == 404


async def test_slug_validation_rejects_bad_slug(client: AsyncClient) -> None:
    resp = await client.post(
        "/collections",
        data={"slug": "Bad Slug!!!", "title": "x", "tag": "y", "public": "0"},
    )
    assert resp.status_code == 400


async def test_ocr_skip_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/settings/ocr-skip")
    assert resp.status_code == 200


async def test_ocr_skip_add_and_remove(client: AsyncClient) -> None:
    from app.storage.ocr_skip import is_skipped, list_skipped

    resp = await client.post("/settings/ocr-skip", data={"app_name": "TestApp"})
    assert resp.status_code in {200, 303, 302}
    assert await is_skipped("testapp")
    assert "testapp" in {s.casefold() for s in await list_skipped()}

    resp = await client.post("/settings/ocr-skip/testapp/delete")
    assert resp.status_code in {200, 303, 302}
    assert not await is_skipped("testapp")


@pytest.mark.asyncio
async def test_backup_module_importable() -> None:
    """Backup module is importable even without cryptography installed."""
    from app.backup import snapshot

    assert hasattr(snapshot, "create_backup")
    assert hasattr(snapshot, "restore_backup")


@pytest.mark.asyncio
async def test_cli_exposes_backup_and_restore() -> None:
    from app import cli

    src = open(cli.__file__, encoding="utf-8").read()
    assert '"backup"' in src
    assert '"restore"' in src
