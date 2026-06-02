"""Tests for tier transitions, size log, and pin/unpin."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
import pytest

from app.settings import get_settings
from app.storage.repository import insert_screenshot
from app.storage.size_log import list_recent, sample_today, today_bytes
from app.storage.tiers import (
    count_by_tier,
    list_by_tier,
    pin_screenshot,
    set_tier,
    unpin_screenshot,
)


@pytest.mark.asyncio
async def test_initial_tier_is_hot(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=10,
        height=10,
        phash="tier0001",
    )
    counts = await count_by_tier(db)
    assert counts.get("hot", 0) >= 1
    rows = await list_by_tier(db, "hot", limit=10)
    assert any(row["id"] == sid for row in rows)


@pytest.mark.asyncio
async def test_set_tier(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=10,
        height=10,
        phash="tier0002",
    )
    await set_tier(db, sid, "cold")
    counts = await count_by_tier(db)
    assert counts.get("cold", 0) == 1


@pytest.mark.asyncio
async def test_pin_unpin(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db,
        captured_at=datetime.now(timezone.utc),
        width=10,
        height=10,
        phash="tier0003",
    )
    await pin_screenshot(db, sid)
    counts = await count_by_tier(db)
    assert counts.get("pinned", 0) == 1

    await unpin_screenshot(db, sid)
    counts = await count_by_tier(db)
    assert counts.get("hot", 0) == 1


@pytest.mark.asyncio
async def test_sample_today_creates_row(db: aiosqlite.Connection) -> None:
    settings = get_settings()
    result = await sample_today(db, settings.thumbnails_dir)
    assert "bytes" in result
    assert "files" in result
    rows = await list_recent(db, days=1)
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_today_bytes_returns_zero_when_no_thumbs(db: aiosqlite.Connection) -> None:
    settings = get_settings()
    await sample_today(db, settings.thumbnails_dir)
    assert await today_bytes(db) == 0
