"""Tests for v0.31 — idle stats + OCR phrase tags + SMTP digest."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.storage.db import init_database


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import idle_stats as idle_stats_routes
    from app.web.routes import ocr_phrase_tags as ocr_phrase_tags_routes
    from app.web.routes import smtp_settings as smtp_settings_routes

    await init_database()
    app = FastAPI()
    app.include_router(idle_stats_routes.router)
    app.include_router(ocr_phrase_tags_routes.router)
    app.include_router(smtp_settings_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


async def test_idle_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/idle")
    assert resp.status_code == 200


async def test_idle_api(client: AsyncClient) -> None:
    resp = await client.get("/api/idle.json?day=2026-06-02")
    assert resp.status_code == 200
    data = resp.json()
    assert "active_seconds" in data
    assert "idle_seconds" in data
    assert "active_shots" in data
    assert "idle_shots" in data


@pytest.mark.asyncio
async def test_daily_idle_empty() -> None:
    await init_database()
    from app.idle_stats import daily_idle

    result = await daily_idle("2026-06-02")
    assert result["active_seconds"] == 0
    assert result["idle_seconds"] == 0
    assert result["active_shots"] == 0
    assert result["idle_shots"] == 0


async def test_phrase_tags_page(client: AsyncClient) -> None:
    resp = await client.get("/settings/phrase-tags")
    assert resp.status_code == 200


async def test_phrase_tag_add(client: AsyncClient) -> None:
    resp = await client.post(
        "/settings/phrase-tags",
        data={"phrase": "daily standup", "tag": "standup", "case_sensitive": "0"},
    )
    assert resp.status_code in {200, 303, 302}


@pytest.mark.asyncio
async def test_apply_phrase_rules() -> None:
    await init_database()
    from app.ocr_phrase_tags import add, apply_phrase_rules

    await add("design review", "design", case_sensitive=False)
    tags = await apply_phrase_rules("Today we have a design review meeting.")
    assert "design" in tags

    tags = await apply_phrase_rules("Just shipping code, no review here.")
    assert "design" not in tags


async def test_smtp_settings_page(client: AsyncClient) -> None:
    resp = await client.get("/settings/smtp")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_smtp_send_disabled_by_default() -> None:
    await init_database()
    from app.smtp_delivery import send_digest_email

    result = await send_digest_email("Test", "body")
    assert result["status"] in {"disabled", "misconfigured", "missing_dep"}
