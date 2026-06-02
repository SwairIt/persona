"""Tests for v0.25 — image-region blur + per-day storage report + notes templates."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import note_templates as note_templates_routes
    from app.web.routes import storage_report as storage_report_routes

    await init_database()
    app = FastAPI()
    app.include_router(storage_report_routes.router)
    app.include_router(note_templates_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_storage_report_renders(client: AsyncClient) -> None:
    resp = await client.get("/storage-report")
    assert resp.status_code == 200
    assert "Date" in resp.text or "date" in resp.text.lower()


@pytest.mark.asyncio
async def test_daily_breakdown_returns_list() -> None:
    await init_database()
    from app.storage_report import daily_breakdown

    rows = await daily_breakdown(days_back=7)
    assert isinstance(rows, list)
    for row in rows:
        assert "date" in row
        assert "total_bytes" in row
        assert isinstance(row["total_bytes"], int)


async def test_note_templates_index(client: AsyncClient) -> None:
    resp = await client.get("/notes/templates")
    assert resp.status_code == 200


async def test_note_templates_seeded(client: AsyncClient) -> None:
    """Migration seeds 3 starter templates."""
    resp = await client.get("/notes/templates")
    assert resp.status_code == 200
    body = resp.text.lower()
    assert "standup" in body or "meeting" in body or "bug" in body


async def test_note_template_apply_returns_body(client: AsyncClient) -> None:
    resp = await client.get("/notes/templates/standup/apply")
    assert resp.status_code == 200
    assert len(resp.text) > 0


async def test_note_template_apply_404(client: AsyncClient) -> None:
    resp = await client.get("/notes/templates/does-not-exist-xyz/apply")
    assert resp.status_code == 404


async def test_note_template_add_and_delete(client: AsyncClient) -> None:
    create = await client.post(
        "/notes/templates",
        data={"slug": "test-tpl", "title": "Test", "body": "Hello"},
    )
    assert create.status_code in {200, 303, 302}

    apply = await client.get("/notes/templates/test-tpl/apply")
    assert apply.status_code == 200
    assert "Hello" in apply.text

    delete = await client.post("/notes/templates/test-tpl/delete")
    assert delete.status_code in {200, 303, 302}


@pytest.mark.asyncio
async def test_image_blur_module_importable() -> None:
    from app import image_blur

    assert hasattr(image_blur, "blur_sensitive_regions")
