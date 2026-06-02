"""Tests for compute_streaks."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import aiosqlite
import pytest

from app.analysis.streak import compute_streaks
from app.storage.repository import insert_screenshot


@pytest.mark.asyncio
async def test_empty_streak(db: aiosqlite.Connection) -> None:
    s = await compute_streaks(db)
    assert s.current_streak == 0
    assert s.longest_streak == 0
    assert s.active_days_total == 0


@pytest.mark.asyncio
async def test_three_day_streak_ending_today(db: aiosqlite.Connection) -> None:
    today = date(2026, 6, 2)
    for delta in range(3):
        d = datetime.combine(today - timedelta(days=delta), datetime.min.time(), tzinfo=timezone.utc)
        await insert_screenshot(
            db, captured_at=d, width=1, height=1, phash=f"strk{delta:012d}"
        )
    s = await compute_streaks(db, today=today)
    assert s.current_streak == 3
    assert s.longest_streak == 3
    assert s.active_days_total == 3


@pytest.mark.asyncio
async def test_broken_streak(db: aiosqlite.Connection) -> None:
    today = date(2026, 6, 2)
    days = [today, today - timedelta(days=1), today - timedelta(days=4), today - timedelta(days=5), today - timedelta(days=6)]
    for i, d in enumerate(days):
        when = datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)
        await insert_screenshot(
            db, captured_at=when, width=1, height=1, phash=f"brkk{i:012d}"
        )
    s = await compute_streaks(db, today=today)
    assert s.current_streak == 2
    assert s.longest_streak == 3


@pytest.mark.asyncio
async def test_streak_uses_yesterday_when_today_empty(db: aiosqlite.Connection) -> None:
    today = date(2026, 6, 2)
    yesterday = today - timedelta(days=1)
    when = datetime.combine(yesterday, datetime.min.time(), tzinfo=timezone.utc)
    await insert_screenshot(db, captured_at=when, width=1, height=1, phash="yest0000000000")
    s = await compute_streaks(db, today=today)
    assert s.current_streak == 1
