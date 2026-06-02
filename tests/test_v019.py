"""Tests for v0.19 — quiet hours + reminder-screenshot link + CLI smoke."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.settings import get_settings
from app.storage.db import init_database
from app.storage.quiet_hours import (
    create_rule,
    delete_rule,
    is_quiet_now,
    list_rules,
)
from app.storage.reminders import (
    create_reminder,
    list_for_day,
    list_for_screenshot,
)
from app.storage.repository import insert_screenshot


@pytest.mark.asyncio
async def test_quiet_hours_crud(db: aiosqlite.Connection) -> None:
    rid = await create_rule(db, weekday=0, start_hour=23, end_hour=24, label="weeknight")
    rules = await list_rules(db)
    assert any(r["id"] == rid and r["label"] == "weeknight" for r in rules)
    await delete_rule(db, rid)
    rules = await list_rules(db)
    assert not any(r["id"] == rid for r in rules)


@pytest.mark.asyncio
async def test_quiet_hours_validates(db: aiosqlite.Connection) -> None:
    with pytest.raises(ValueError):
        await create_rule(db, weekday=9, start_hour=0, end_hour=1, label="bad")
    with pytest.raises(ValueError):
        await create_rule(db, weekday=0, start_hour=10, end_hour=10, label="zero-len")
    with pytest.raises(ValueError):
        await create_rule(db, weekday=0, start_hour=24, end_hour=25, label="overflow")


@pytest.mark.asyncio
async def test_is_quiet_now_matches(db: aiosqlite.Connection) -> None:
    # craft a "now" that's Monday 23:30 local
    fake_now = datetime(2026, 6, 1, 23, 30).astimezone()
    await create_rule(db, weekday=0, start_hour=23, end_hour=24, label="late")
    assert await is_quiet_now(db, now=fake_now) is True

    # daytime same Monday → not quiet
    daytime = datetime(2026, 6, 1, 12, 0).astimezone()
    assert await is_quiet_now(db, now=daytime) is False


@pytest.mark.asyncio
async def test_is_quiet_now_empty(db: aiosqlite.Connection) -> None:
    assert await is_quiet_now(db) is False


@pytest.mark.asyncio
async def test_reminder_with_screenshot_link(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=1,
        height=1,
        phash="rlnk000000000001",
    )
    rid = await create_reminder(
        db,
        body="review this",
        due_date=date.today(),
        screenshot_id=sid,
    )
    items = await list_for_day(db, day=date.today())
    assert any(i["id"] == rid and i.get("screenshot_id") == sid for i in items)

    for_shot = await list_for_screenshot(db, sid)
    assert any(i["id"] == rid for i in for_shot)


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    from fastapi import FastAPI

    from app.web.routes import quiet_hours as quiet_hours_routes
    from app.web.routes import reminders as reminders_routes

    await init_database()
    app = FastAPI()
    app.include_router(quiet_hours_routes.router)
    app.include_router(reminders_routes.router)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def test_quiet_hours_page_renders(client: AsyncClient) -> None:
    resp = await client.get("/quiet-hours")
    assert resp.status_code == 200


async def test_quiet_hours_create_via_api(client: AsyncClient) -> None:
    resp = await client.post(
        "/quiet-hours",
        data={"weekday": "5", "start_hour": "0", "end_hour": "24", "label": "saturday"},
        follow_redirects=False,
    )
    assert resp.status_code in {303, 307}


async def test_remind_endpoint(client: AsyncClient) -> None:
    async with aiosqlite.connect(get_settings().db_path) as conn:
        conn.row_factory = aiosqlite.Row
        sid = await insert_screenshot(
            conn,
            captured_at=datetime.now(timezone.utc),
            width=1,
            height=1,
            phash="remind000000000001",
        )

    resp = await client.post(
        f"/api/screenshots/{sid}/remind",
        data={"body": "review tomorrow", "due_date": (date.today() + timedelta(days=1)).isoformat()},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert "reminder_id" in data


@pytest.mark.asyncio
async def test_cli_imports() -> None:
    """Smoke test — CLI module imports without errors and exposes `main`."""
    from app import cli

    assert callable(cli.main)
