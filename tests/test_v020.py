"""Tests for v0.20 — share-collection + OCR reprocess + webhook test-fire."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.settings import get_settings
from app.storage.db import init_database
from app.storage.ocr_admin import (
    reset_all_to_pending,
    reset_failed_to_pending,
    reset_one,
    reset_skipped_to_pending,
    status_breakdown,
)
from app.storage.repository import insert_screenshot, update_screenshot_ocr
from app.storage.webhooks import create_webhook


@pytest.mark.asyncio
async def test_ocr_reset_skipped(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=1,
        height=1,
        phash="ocr000000000001",
        thumbnail_path="/fake/thumb.webp",
        ocr_status="skipped",
    )
    moved = await reset_skipped_to_pending(db)
    assert moved >= 1
    breakdown = await status_breakdown(db)
    assert breakdown.get("pending", 0) >= 1


@pytest.mark.asyncio
async def test_ocr_reset_skipped_skips_no_thumb(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=1,
        height=1,
        phash="ocr000000000002",
        thumbnail_path=None,
        ocr_status="skipped",
    )
    await reset_skipped_to_pending(db)
    cursor = await db.execute(
        "SELECT ocr_status FROM screenshots WHERE id = ?", (sid,)
    )
    row = await cursor.fetchone()
    assert row["ocr_status"] == "skipped"


@pytest.mark.asyncio
async def test_ocr_reset_failed(db: aiosqlite.Connection) -> None:
    await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=1,
        height=1,
        phash="ocr000000000003",
        thumbnail_path="/fake/thumb.webp",
        ocr_status="failed",
    )
    moved = await reset_failed_to_pending(db)
    assert moved >= 1


@pytest.mark.asyncio
async def test_ocr_reset_one(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=1,
        height=1,
        phash="ocr000000000004",
        thumbnail_path="/fake/thumb.webp",
        ocr_status="done",
    )
    assert await reset_one(db, sid) is True
    cursor = await db.execute(
        "SELECT ocr_status FROM screenshots WHERE id = ?", (sid,)
    )
    row = await cursor.fetchone()
    assert row["ocr_status"] == "pending"

    assert await reset_one(db, 999999) is False


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.auth import SESSION_COOKIE_NAME, issue_session
    from app.web.routes import ocr_admin as ocr_admin_routes
    from app.web.routes import share_collection as share_collection_routes
    from app.web.routes import webhooks_routes

    await init_database()
    # Ф (security, 2026-06-24): share-collection + webhook test-fire — owner-only.
    # Создаём владельца + сессию и шлём cookie, иначе 303 на логин.
    async with aiosqlite.connect(get_settings().db_path) as conn:
        await conn.execute(
            "INSERT OR IGNORE INTO users(id,email,password_hash) VALUES(1,'t@x.c','x')"
        )
        await conn.commit()
    token, _ = await issue_session(1)
    app = FastAPI()
    app.include_router(share_collection_routes.router)
    app.include_router(ocr_admin_routes.router)
    app.include_router(webhooks_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test",
        cookies={SESSION_COOKIE_NAME: token},
    ) as ac:
        yield ac


async def test_share_collection_roundtrip(client: AsyncClient) -> None:
    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        ids = [
            await insert_screenshot(
                conn,
                captured_at=datetime.now(timezone.utc),
                width=1,
                height=1,
                phash=f"sc{i:014d}",
                app_name="App",
            )
            for i in range(3)
        ]

    resp = await client.post(
        "/api/share/collection",
        data={"screenshot_ids": ",".join(str(i) for i in ids), "title": "v0.20 test", "ttl_hours": "1"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "url" in data
    url = data["url"]

    resp = await client.get(url)
    # v1.0+ share now requires a passcode by default; 403 with link
    # info is a valid response. Accept both modes.
    assert resp.status_code in {200, 403}
    if resp.status_code == 200:
        assert "v0.20 test" in resp.text


async def test_share_collection_invalid_token(client: AsyncClient) -> None:
    resp = await client.get("/share/collection/garbage.token")
    assert resp.status_code in {400, 403, 404}


async def test_ocr_admin_page(client: AsyncClient) -> None:
    resp = await client.get("/ocr-admin")
    assert resp.status_code == 200


async def test_webhook_test_fire(client: AsyncClient) -> None:
    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        wid = await create_webhook(
            conn,
            url="https://hook.example.com/persona",
            event_type="capture.saved",
        )

    resp = await client.post(f"/api/webhooks/{wid}/test")
    assert resp.status_code == 200
    data = resp.json()
    assert data["webhook_id"] == wid
    assert data["event_type"] == "capture.saved"
    assert data["queued"] is True
