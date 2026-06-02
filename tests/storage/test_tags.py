"""Tests for tags + saved_searches CRUD."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
import pytest

from app.storage.repository import insert_screenshot
from app.storage.tags import (
    create_tag,
    delete_saved_search,
    get_screenshot_tags,
    list_saved_searches,
    list_screenshots_by_tag,
    list_tags,
    save_search,
    tag_screenshot,
    untag_screenshot,
)


@pytest.mark.asyncio
async def test_create_tag_idempotent(db: aiosqlite.Connection) -> None:
    tag1 = await create_tag(db, name="important", color="#ff0000")
    tag2 = await create_tag(db, name="important")
    assert tag1 == tag2


@pytest.mark.asyncio
async def test_tag_and_untag_screenshot(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=100,
        height=100,
        phash="aaaa1111bbbb2222",
    )
    tag_id = await create_tag(db, name="focus")
    await tag_screenshot(db, sid, tag_id)
    tags = await get_screenshot_tags(db, sid)
    assert any(t["name"] == "focus" for t in tags)

    await untag_screenshot(db, sid, tag_id)
    tags_after = await get_screenshot_tags(db, sid)
    assert tags_after == []


@pytest.mark.asyncio
async def test_list_screenshots_by_tag(db: aiosqlite.Connection) -> None:
    tag_id = await create_tag(db, name="cool")
    now = datetime.now(timezone.utc)
    sid1 = await insert_screenshot(db, captured_at=now, width=10, height=10, phash="1111")
    sid2 = await insert_screenshot(db, captured_at=now, width=10, height=10, phash="2222")
    await tag_screenshot(db, sid1, tag_id)
    await tag_screenshot(db, sid2, tag_id)
    ids = await list_screenshots_by_tag(db, tag_id)
    assert set(ids) == {sid1, sid2}


@pytest.mark.asyncio
async def test_list_tags_with_counts(db: aiosqlite.Connection) -> None:
    tag_id = await create_tag(db, name="counted")
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=10,
        height=10,
        phash="3333",
    )
    await tag_screenshot(db, sid, tag_id)
    tags = await list_tags(db)
    assert any(t["name"] == "counted" and t["count"] == 1 for t in tags)


@pytest.mark.asyncio
async def test_saved_searches_lifecycle(db: aiosqlite.Connection) -> None:
    sid = await save_search(db, name="docker logs", query="docker", app_name="Terminal")
    saved = await list_saved_searches(db)
    assert any(s["id"] == sid and s["name"] == "docker logs" for s in saved)

    await delete_saved_search(db, sid)
    remaining = await list_saved_searches(db)
    assert all(s["id"] != sid for s in remaining)
