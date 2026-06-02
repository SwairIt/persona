"""Tests for the per-app time-sheet helper."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import aiosqlite
import pytest

from app.analysis.time_sheet import (
    compute_per_app_seconds,
    format_duration,
    per_day_total_seconds,
)
from app.storage.repository import insert_screenshot


def test_format_duration() -> None:
    assert format_duration(0) == "0s"
    assert format_duration(45) == "45s"
    assert format_duration(60) == "1m"
    assert format_duration(125) == "2m"
    assert format_duration(3600) == "1h"
    assert format_duration(3660) == "1h 1m"


@pytest.mark.asyncio
async def test_empty_day(db: aiosqlite.Connection) -> None:
    items = await compute_per_app_seconds(db, day=date(2026, 6, 2))
    assert items == []


@pytest.mark.asyncio
async def test_two_app_session(db: aiosqlite.Connection) -> None:
    base = datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc)
    for delta in range(5):
        await insert_screenshot(
            db,
            captured_at=base + timedelta(minutes=delta),
            width=10,
            height=10,
            phash=f"ts{delta:014d}",
            app_name="VS Code",
        )
    items = await compute_per_app_seconds(db, day=date(2026, 6, 2))
    assert len(items) == 1
    assert items[0].app_name == "VS Code"
    assert items[0].seconds >= 4 * 60


@pytest.mark.asyncio
async def test_idle_gap_does_not_count(db: aiosqlite.Connection) -> None:
    base = datetime(2026, 6, 2, 10, 0, tzinfo=timezone.utc)
    await insert_screenshot(db, captured_at=base, width=10, height=10, phash="tsg0001", app_name="App")
    far_later = base + timedelta(hours=2)
    await insert_screenshot(db, captured_at=far_later, width=10, height=10, phash="tsg0002", app_name="App")
    items = await compute_per_app_seconds(db, day=date(2026, 6, 2), idle_gap_seconds=60)
    # both should fall back to tick_seconds
    assert items[0].seconds < 60


@pytest.mark.asyncio
async def test_per_day_totals(db: aiosqlite.Connection) -> None:
    today = datetime.now(timezone.utc)
    for d in range(3):
        ts = today - timedelta(days=d, hours=2)
        await insert_screenshot(db, captured_at=ts, width=10, height=10, phash=f"pdd{d:014d}", app_name="A")
    totals = await per_day_total_seconds(db, days=10)
    assert len(totals) >= 3
    assert all(v > 0 for v in totals.values())
