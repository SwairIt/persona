"""Tests for screenshot notes CRUD."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
import pytest

from app.storage.notes import delete_note, get_note, upsert_note
from app.storage.repository import insert_screenshot


@pytest.mark.asyncio
async def test_get_note_returns_none_when_absent(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db, captured_at=datetime.now(timezone.utc), width=10, height=10, phash="aaaa"
    )
    assert await get_note(db, sid) is None


@pytest.mark.asyncio
async def test_upsert_creates_and_updates(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db, captured_at=datetime.now(timezone.utc), width=10, height=10, phash="bbbb"
    )
    await upsert_note(db, sid, "first version")
    assert await get_note(db, sid) == "first version"

    await upsert_note(db, sid, "second version")
    assert await get_note(db, sid) == "second version"


@pytest.mark.asyncio
async def test_delete_removes_note(db: aiosqlite.Connection) -> None:
    sid = await insert_screenshot(
        db, captured_at=datetime.now(timezone.utc), width=10, height=10, phash="cccc"
    )
    await upsert_note(db, sid, "x")
    await delete_note(db, sid)
    assert await get_note(db, sid) is None
