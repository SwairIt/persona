"""Tests for v0.44 — webhook event filters + OCR near-dup + public day."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import ocr_near_dup as ocr_near_dup_routes
    from app.web.routes import public_day as public_day_routes

    await init_database()
    app = FastAPI()
    app.include_router(ocr_near_dup_routes.router)
    app.include_router(public_day_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


@pytest.mark.asyncio
async def test_webhook_filter_star_matches_all() -> None:
    from app.webhook_filters import should_fire

    assert should_fire("*", "screenshot.captured")
    assert should_fire("", "any.event")
    assert should_fire(None, "any.event")


@pytest.mark.asyncio
async def test_webhook_filter_exact_match() -> None:
    from app.webhook_filters import should_fire

    assert should_fire("screenshot.captured", "screenshot.captured")
    assert not should_fire("screenshot.captured", "screenshot.tagged")


@pytest.mark.asyncio
async def test_webhook_filter_glob_prefix() -> None:
    from app.webhook_filters import should_fire

    assert should_fire("screenshot.*", "screenshot.captured")
    assert should_fire("screenshot.*", "screenshot.tagged")
    assert not should_fire("screenshot.*", "ocr.done")


@pytest.mark.asyncio
async def test_webhook_filter_csv_list() -> None:
    from app.webhook_filters import should_fire

    assert should_fire("screenshot.captured, ocr.done", "ocr.done")
    assert not should_fire("screenshot.captured, ocr.done", "weekly_digest.generated")


async def test_ocr_near_dup_page(client: AsyncClient) -> None:
    resp = await client.get("/admin/ocr-near-duplicates")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_find_near_duplicates_empty() -> None:
    await init_database()
    from app.ocr_near_dup import find_near_duplicates

    pairs = await find_near_duplicates(days=7, min_jaccard=0.85)
    assert isinstance(pairs, list)
    assert len(pairs) == 0


async def test_public_day_admin_renders(client: AsyncClient) -> None:
    resp = await client.get("/admin/public-days")
    assert resp.status_code == 200


async def test_public_day_unknown_slug_404(client: AsyncClient) -> None:
    resp = await client.get("/public/day/does-not-exist-xyz")
    assert resp.status_code == 404


async def test_public_day_publish_and_view(client: AsyncClient) -> None:
    publish = await client.post(
        "/admin/public-days",
        data={
            "day": "2026-06-02",
            "slug": "test-day",
            "title": "Test Day",
            "blurb": "Demo content",
        },
    )
    assert publish.status_code in {200, 303, 302}

    view = await client.get("/public/day/test-day")
    assert view.status_code == 200
    assert "Test Day" in view.text or "test-day" in view.text


async def test_public_day_bad_slug_rejected(client: AsyncClient) -> None:
    resp = await client.post(
        "/admin/public-days",
        data={"day": "2026-06-02", "slug": "Bad Slug!!", "title": "x", "blurb": ""},
    )
    assert resp.status_code == 400
