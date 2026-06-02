"""Tests for tag colour edit + saved-search seen tracking."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
import pytest

from app.search import search as run_fts_search
from app.storage.repository import insert_screenshot
from app.storage.tags import (
    create_tag,
    list_tags,
    save_search,
    saved_search_mark_seen,
    saved_search_new_count,
    set_tag_color,
)


@pytest.mark.asyncio
async def test_set_tag_color(db: aiosqlite.Connection) -> None:
    tid = await create_tag(db, name="coloured")
    await set_tag_color(db, tid, color="#ff0080")
    tag = next(t for t in await list_tags(db) if t["id"] == tid)
    assert tag["color"] == "#ff0080"


@pytest.mark.asyncio
async def test_set_tag_color_invalid_rejected(db: aiosqlite.Connection) -> None:
    tid = await create_tag(db, name="invalid")
    with pytest.raises(ValueError):
        await set_tag_color(db, tid, color="rgb(1,2,3)")


@pytest.mark.asyncio
async def test_set_tag_color_clear(db: aiosqlite.Connection) -> None:
    tid = await create_tag(db, name="clearable", color="#abcdef")
    await set_tag_color(db, tid, color=None)
    tag = next(t for t in await list_tags(db) if t["id"] == tid)
    assert tag["color"] is None


@pytest.mark.asyncio
async def test_saved_search_new_count(db: aiosqlite.Connection) -> None:
    sid = await save_search(db, name="auth-watch", query="auth")

    base = datetime.now(timezone.utc)
    for i in range(3):
        await insert_screenshot(
            db,
            captured_at=base,
            width=1,
            height=1,
            phash=f"ss{i:014d}",
            app_name="Slack",
            window_title=f"auth talk {i}",
        )

    count_before = await saved_search_new_count(
        db, search_id=sid, fts_query_callback=run_fts_search
    )
    assert count_before == 3

    cursor = await db.execute("SELECT MAX(id) AS max_id FROM screenshots")
    row = await cursor.fetchone()
    assert row is not None
    await saved_search_mark_seen(db, search_id=sid, highest_id=int(row["max_id"]))

    count_after = await saved_search_new_count(
        db, search_id=sid, fts_query_callback=run_fts_search
    )
    assert count_after == 0
