"""Tests for v0.21 — archive browse + regex auto-tag + search history."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.settings import get_settings
from app.storage.db import init_database
from app.storage.regex_rules import (
    apply_rules_to_ocr,
    create_rule,
    delete_rule,
    list_rules,
    toggle_rule,
)
from app.storage.repository import insert_screenshot
from app.storage.search_history import clear_history, list_recent, record_query
from app.storage.tags import get_screenshot_tags


@pytest.mark.asyncio
async def test_regex_create_invalid_pattern(db: aiosqlite.Connection) -> None:
    with pytest.raises(ValueError):
        await create_rule(db, pattern="(unbalanced", tag_name="bad")
    with pytest.raises(ValueError):
        await create_rule(db, pattern="ok", tag_name="")


@pytest.mark.asyncio
async def test_regex_create_list_toggle_delete(db: aiosqlite.Connection) -> None:
    rid = await create_rule(db, pattern=r"invoice", tag_name="invoice")
    rules = await list_rules(db)
    assert any(r["id"] == rid for r in rules)

    await toggle_rule(db, rid, enabled=False)
    rules = await list_rules(db, only_enabled=True)
    assert not any(r["id"] == rid for r in rules)

    await toggle_rule(db, rid, enabled=True)
    await delete_rule(db, rid)
    rules = await list_rules(db)
    assert not any(r["id"] == rid for r in rules)


@pytest.mark.asyncio
async def test_apply_rules_creates_tags(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=1,
        height=1,
        phash="rgx000000000001",
    )
    await create_rule(db, pattern=r"invoice", tag_name="invoice", case_insensitive=True)
    await create_rule(db, pattern=r"meeting", tag_name="meeting")

    applied = await apply_rules_to_ocr(
        db, screenshot_id=sid, ocr_text="INVOICE #123 from Acme — schedule a meeting tomorrow"
    )
    assert "invoice" in applied
    assert "meeting" in applied

    tags = await get_screenshot_tags(db, sid)
    names = {t["name"] for t in tags}
    assert {"invoice", "meeting"} <= names


@pytest.mark.asyncio
async def test_apply_rules_disabled_skipped(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=1,
        height=1,
        phash="rgx000000000002",
    )
    rid = await create_rule(db, pattern=r"hello", tag_name="hi")
    await toggle_rule(db, rid, enabled=False)
    applied = await apply_rules_to_ocr(db, screenshot_id=sid, ocr_text="hello world")
    assert applied == []


@pytest.mark.asyncio
async def test_search_history_record_and_list(db: aiosqlite.Connection) -> None:
    await record_query(db, query="auth", mode="hybrid")
    await record_query(db, query="invoice", mode="fts")
    await record_query(db, query="auth", mode="hybrid")
    recent = await list_recent(db, limit=10)
    queries = [r["query"] for r in recent]
    assert "auth" in queries
    assert "invoice" in queries
    auth_entry = next(r for r in recent if r["query"] == "auth")
    assert auth_entry["use_count"] >= 2


@pytest.mark.asyncio
async def test_search_history_clear(db: aiosqlite.Connection) -> None:
    await record_query(db, query="x", mode="hybrid")
    await record_query(db, query="y", mode="hybrid")
    deleted = await clear_history(db)
    assert deleted >= 2
    assert await list_recent(db) == []


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import archive_browse as archive_browse_routes
    from app.web.routes import regex_rules as regex_rules_routes

    await init_database()
    app = FastAPI()
    app.include_router(archive_browse_routes.router)
    app.include_router(regex_rules_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_archive_browse_page(client: AsyncClient) -> None:
    resp = await client.get("/archive/browse")
    assert resp.status_code == 200


async def test_archive_search_page_empty_query(client: AsyncClient) -> None:
    resp = await client.get("/archive/search")
    assert resp.status_code == 200


async def test_regex_rules_page(client: AsyncClient) -> None:
    resp = await client.get("/regex-rules")
    assert resp.status_code == 200


async def test_regex_rules_test_endpoint(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/regex-rules/test",
        data={"pattern": r"\d+", "text": "id 42 ok 99", "case_insensitive": "on"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "42" in data["matches"]
    assert "99" in data["matches"]


async def test_regex_rules_test_invalid(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/regex-rules/test",
        data={"pattern": r"(unbalanced", "text": "hi", "case_insensitive": "on"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "error" in data
