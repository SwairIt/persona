"""Tests for storage CRUD helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from app.storage.repository import (
    bump_dedup_group,
    find_dedup_group_by_phash,
    get_kv,
    get_screenshot,
    insert_dedup_group,
    insert_screenshot,
    list_capture_events,
    list_kv,
    list_pending_ocr,
    list_screenshots,
    log_capture_event,
    mark_pending_ocr_as_skipped,
    set_dedup_group_representative,
    set_kv,
    update_screenshot_ocr,
)


@pytest.mark.asyncio
async def test_insert_and_get_screenshot(db: aiosqlite.Connection) -> None:
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    sid = await insert_screenshot(
        db,
        captured_at=now,
        width=1920,
        height=1080,
        phash="abcd1234abcd1234",
        app_name="VS Code",
        window_title="main.py",
        process_name="code.exe",
    )
    assert sid > 0

    shot = await get_screenshot(db, sid)
    assert shot is not None
    assert shot.id == sid
    assert shot.width == 1920
    assert shot.app_name == "VS Code"
    assert shot.ocr_status == "pending"


@pytest.mark.asyncio
async def test_list_screenshots_filters_by_date_range(db: aiosqlite.Connection) -> None:
    base = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    for delta in range(10):
        await insert_screenshot(
            db,
            captured_at=base + timedelta(minutes=delta),
            width=800,
            height=600,
            phash=f"hash{delta:012d}",
        )

    in_range = await list_screenshots(
        db,
        since=base + timedelta(minutes=2),
        until=base + timedelta(minutes=5),
    )
    assert len(in_range) == 3


@pytest.mark.asyncio
async def test_dedup_group_lifecycle(db: aiosqlite.Connection) -> None:
    now = datetime.now(timezone.utc)
    gid = await insert_dedup_group(db, phash="ffff0000ffff0000", representative_screenshot_id=None, first_seen=now)

    found = await find_dedup_group_by_phash(db, "ffff0000ffff0000")
    assert found is not None
    assert found.id == gid
    assert found.seen_count == 1

    await bump_dedup_group(db, gid, last_seen=now + timedelta(seconds=10))
    bumped = await find_dedup_group_by_phash(db, "ffff0000ffff0000")
    assert bumped is not None
    assert bumped.seen_count == 2


@pytest.mark.asyncio
async def test_set_representative_and_ocr_update(db: aiosqlite.Connection) -> None:
    now = datetime.now(timezone.utc)
    gid = await insert_dedup_group(db, phash="0011223344556677", representative_screenshot_id=None, first_seen=now)
    sid = await insert_screenshot(
        db,
        captured_at=now,
        width=100,
        height=100,
        phash="0011223344556677",
        dedup_group_id=gid,
    )
    await set_dedup_group_representative(db, gid, sid)

    await update_screenshot_ocr(db, sid, ocr_text="hello world", ocr_status="done")
    shot = await get_screenshot(db, sid)
    assert shot is not None
    assert shot.ocr_text == "hello world"
    assert shot.ocr_status == "done"


@pytest.mark.asyncio
async def test_pending_ocr_queue(db: aiosqlite.Connection) -> None:
    now = datetime.now(timezone.utc)
    for i in range(3):
        await insert_screenshot(
            db,
            captured_at=now + timedelta(seconds=i),
            width=100,
            height=100,
            phash=f"{i:016d}",
            ocr_status="pending",
        )
    pending = await list_pending_ocr(db, limit=10)
    assert len(pending) == 3
    assert all(s.ocr_status == "pending" for s in pending)

    skipped = await mark_pending_ocr_as_skipped(db)
    assert skipped == 3
    remaining = await list_pending_ocr(db)
    assert remaining == []


@pytest.mark.asyncio
async def test_capture_event_log(db: aiosqlite.Connection) -> None:
    await log_capture_event(db, "start", {"v": 1})
    await log_capture_event(db, "heartbeat")
    events = await list_capture_events(db)
    assert len(events) == 2
    assert events[0].event_type in {"start", "heartbeat"}


@pytest.mark.asyncio
async def test_kv_settings(db: aiosqlite.Connection) -> None:
    assert await get_kv(db, "missing") is None
    await set_kv(db, "theme", "dark")
    await set_kv(db, "lang", "en")
    await set_kv(db, "theme", "light")
    assert await get_kv(db, "theme") == "light"
    assert await get_kv(db, "lang") == "en"
    all_kv = await list_kv(db)
    assert all_kv["theme"] == "light"
    assert all_kv["lang"] == "en"
