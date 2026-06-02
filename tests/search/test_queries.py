"""Tests for FTS5 search."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from app.search.queries import _sanitise_query, search
from app.storage.repository import insert_screenshot, update_screenshot_ocr


def test_sanitise_query_basic() -> None:
    assert _sanitise_query("hello world") == "hello* world*"
    assert _sanitise_query("") == ""
    assert _sanitise_query("'\"&^%") in {'"', ""}


def test_sanitise_query_phrase_passthrough() -> None:
    assert "exact" in _sanitise_query('"exact phrase"')


@pytest.mark.asyncio
async def test_search_finds_by_window_title(db: aiosqlite.Connection) -> None:
    now = datetime.now(timezone.utc)
    sid = await insert_screenshot(
        db,
        captured_at=now,
        width=100,
        height=100,
        phash="0011223344556677",
        app_name="VS Code",
        window_title="main.py — persona",
    )
    hits = await search(db, query="persona", limit=10)
    assert len(hits) == 1
    assert hits[0].screenshot_id == sid


@pytest.mark.asyncio
async def test_search_finds_by_ocr_text(db: aiosqlite.Connection) -> None:
    now = datetime.now(timezone.utc)
    sid = await insert_screenshot(
        db,
        captured_at=now,
        width=100,
        height=100,
        phash="aabbccddeeff0011",
        app_name="Slack",
        window_title="general",
    )
    await update_screenshot_ocr(db, sid, ocr_text="meeting at 14:30 about auth migration", ocr_status="done")

    hits = await search(db, query="migration", limit=10)
    assert len(hits) == 1
    assert hits[0].screenshot_id == sid


@pytest.mark.asyncio
async def test_search_empty_query_returns_recent(db: aiosqlite.Connection) -> None:
    now = datetime.now(timezone.utc)
    for i in range(3):
        await insert_screenshot(
            db,
            captured_at=now + timedelta(seconds=i),
            width=10,
            height=10,
            phash=f"{i:016d}",
            app_name="App",
        )
    hits = await search(db, query="", limit=10)
    assert len(hits) == 3


@pytest.mark.asyncio
async def test_search_filters_by_app(db: aiosqlite.Connection) -> None:
    now = datetime.now(timezone.utc)
    await insert_screenshot(
        db,
        captured_at=now,
        width=10,
        height=10,
        phash="1111111111111111",
        app_name="Slack",
        window_title="hello",
    )
    await insert_screenshot(
        db,
        captured_at=now,
        width=10,
        height=10,
        phash="2222222222222222",
        app_name="Discord",
        window_title="hello",
    )
    hits = await search(db, query="hello", limit=10, app_name="Slack")
    assert len(hits) == 1
    assert hits[0].app_name == "Slack"
