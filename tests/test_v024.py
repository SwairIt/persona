"""Tests for v0.24 — bulk-tag CLI + OCR text redaction + RSS-per-collection."""

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
    from app.web.routes import redaction as redaction_routes
    from app.web.routes import rss as rss_routes

    await init_database()
    app = FastAPI()
    app.include_router(redaction_routes.router)
    app.include_router(auto_collections_routes.router)
    app.include_router(rss_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as ac:
        yield ac


@pytest.mark.asyncio
async def test_redaction_masks_email() -> None:
    await init_database()
    from app.redaction import apply_redaction

    cleaned, n = await apply_redaction("Contact me at alice@example.com today.")
    assert n >= 1
    assert "alice@example.com" not in cleaned
    assert "***" in cleaned


@pytest.mark.asyncio
async def test_redaction_no_match_returns_unchanged() -> None:
    await init_database()
    from app.redaction import apply_redaction

    cleaned, n = await apply_redaction("hello world no secrets here")
    assert cleaned == "hello world no secrets here"
    assert n == 0


async def test_redaction_settings_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/settings/redaction")
    assert resp.status_code == 200


async def test_redaction_add_rule(client: AsyncClient) -> None:
    resp = await client.post(
        "/settings/redaction",
        data={"name": "ssn_test", "pattern": r"\d{3}-\d{2}-\d{4}"},
    )
    assert resp.status_code in {200, 303, 302}


async def test_collection_rss_404_on_unknown(client: AsyncClient) -> None:
    resp = await client.get("/collection/does-not-exist-xyz.rss")
    assert resp.status_code == 404


async def test_collection_rss_returns_xml_when_rule_exists(client: AsyncClient) -> None:
    create = await client.post(
        "/collections",
        data={"slug": "rss-test", "title": "RSS test", "tag": "rss-tag", "public": "1"},
    )
    assert create.status_code in {200, 303, 302}

    resp = await client.get("/collection/rss-test.rss")
    assert resp.status_code == 200
    assert "xml" in resp.headers.get("content-type", "")
    body = resp.text
    assert "<rss" in body or "<feed" in body
    assert "rss-test" in body or "RSS test" in body


@pytest.mark.asyncio
async def test_bulk_tag_module_importable() -> None:
    from app import bulk_tag

    assert hasattr(bulk_tag, "bulk_tag")
    assert hasattr(bulk_tag, "bulk_untag")


@pytest.mark.asyncio
async def test_cli_exposes_tag_subcommand() -> None:
    from app import cli

    src = open(cli.__file__, encoding="utf-8").read()
    assert '"tag"' in src
    assert '"untag"' in src
