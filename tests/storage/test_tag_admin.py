"""Tests for tag rename / merge / delete / co-tag / per-day."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
import pytest

from app.storage.repository import insert_screenshot
from app.storage.tags import (
    co_tag_counts,
    create_tag,
    delete_tag,
    list_screenshots_by_tag,
    list_tags,
    merge_tag,
    per_day_for_tag,
    rename_tag,
    tag_screenshot,
)


@pytest.mark.asyncio
async def test_rename_simple(db: aiosqlite.Connection) -> None:
    tid = await create_tag(db, name="auth")
    await rename_tag(db, tid, new_name="auth-bug")
    tags = await list_tags(db)
    names = [t["name"] for t in tags]
    assert "auth-bug" in names
    assert "auth" not in names


@pytest.mark.asyncio
async def test_rename_into_existing_merges(db: aiosqlite.Connection) -> None:
    src = await create_tag(db, name="auth")
    dst = await create_tag(db, name="auth-bug")
    sid = await insert_screenshot(
        db, captured_at=datetime.now(timezone.utc), width=1, height=1, phash="rn0001"
    )
    await tag_screenshot(db, sid, src)

    await rename_tag(db, src, new_name="auth-bug")

    tags = await list_tags(db)
    names = [t["name"] for t in tags]
    assert "auth-bug" in names
    assert "auth" not in names
    members = await list_screenshots_by_tag(db, dst)
    assert sid in members


@pytest.mark.asyncio
async def test_merge_explicit(db: aiosqlite.Connection) -> None:
    a = await create_tag(db, name="x")
    b = await create_tag(db, name="y")
    sid = await insert_screenshot(
        db, captured_at=datetime.now(timezone.utc), width=1, height=1, phash="mg0001"
    )
    await tag_screenshot(db, sid, a)
    moved = await merge_tag(db, source_id=a, target_id=b)
    assert moved == 1
    members = await list_screenshots_by_tag(db, b)
    assert sid in members
    tags = await list_tags(db)
    assert "x" not in [t["name"] for t in tags]


@pytest.mark.asyncio
async def test_merge_self_returns_zero(db: aiosqlite.Connection) -> None:
    a = await create_tag(db, name="self-merge")
    assert await merge_tag(db, source_id=a, target_id=a) == 0


@pytest.mark.asyncio
async def test_delete_tag_removes_bindings(db: aiosqlite.Connection) -> None:
    tid = await create_tag(db, name="deleteme")
    sid = await insert_screenshot(
        db, captured_at=datetime.now(timezone.utc), width=1, height=1, phash="dl0001"
    )
    await tag_screenshot(db, sid, tid)
    await delete_tag(db, tid)
    tags = await list_tags(db)
    assert "deleteme" not in [t["name"] for t in tags]
    members = await list_screenshots_by_tag(db, tid)
    assert members == []


@pytest.mark.asyncio
async def test_co_tag_counts(db: aiosqlite.Connection) -> None:
    a = await create_tag(db, name="auth")
    b = await create_tag(db, name="bug")
    c = await create_tag(db, name="ui")
    sid1 = await insert_screenshot(
        db, captured_at=datetime.now(timezone.utc), width=1, height=1, phash="ct0001"
    )
    sid2 = await insert_screenshot(
        db, captured_at=datetime.now(timezone.utc), width=1, height=1, phash="ct0002"
    )
    for sid in (sid1, sid2):
        await tag_screenshot(db, sid, a)
        await tag_screenshot(db, sid, b)
    await tag_screenshot(db, sid1, c)

    co = await co_tag_counts(db, a)
    names_in_order = [item["name"] for item in co]
    assert names_in_order[0] == "bug"
    assert "ui" in names_in_order


@pytest.mark.asyncio
async def test_per_day_for_tag(db: aiosqlite.Connection) -> None:
    tid = await create_tag(db, name="anytag")
    now = datetime.now(timezone.utc)
    sid = await insert_screenshot(
        db, captured_at=now, width=1, height=1, phash="pd0001"
    )
    await tag_screenshot(db, sid, tid)
    rows = await per_day_for_tag(db, tid, days=30)
    assert len(rows) >= 1
