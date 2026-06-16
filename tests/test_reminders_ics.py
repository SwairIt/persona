"""Экспорт напоминаний в .ics (ROADMAP S4a)."""

from __future__ import annotations

from datetime import date, timedelta

import aiosqlite
import pytest

from app.reminders_ics import build_todo_ics
from app.storage.reminders import create_reminder, toggle_done


@pytest.mark.asyncio
async def test_ics_basic_structure(db: aiosqlite.Connection) -> None:
    await create_reminder(db, body="оплатить хостинг", due_date=date.today() + timedelta(days=1))
    ics = await build_todo_ics("localhost")
    assert ics.startswith("BEGIN:VCALENDAR")
    assert ics.rstrip().endswith("END:VCALENDAR")
    assert "\r\n" in ics  # CRLF по RFC 5545
    assert ics.count("BEGIN:VEVENT") == 1
    assert "DTSTART;VALUE=DATE:" in ics  # весьдень-событие
    assert "оплатить хостинг" in ics


@pytest.mark.asyncio
async def test_ics_escapes_special_chars(db: aiosqlite.Connection) -> None:
    await create_reminder(db, body="созвон; важно, не забыть", due_date=date.today())
    ics = await build_todo_ics("localhost")
    assert "созвон\\; важно\\, не забыть" in ics  # ; и , экранированы


@pytest.mark.asyncio
async def test_ics_excludes_done_by_default(db: aiosqlite.Connection) -> None:
    rid = await create_reminder(db, body="готово", due_date=date.today())
    await create_reminder(db, body="активно", due_date=date.today())
    await toggle_done(db, rid, True)
    active = await build_todo_ics("localhost")
    assert active.count("BEGIN:VEVENT") == 1
    assert "активно" in active and "готово" not in active
    # include_done=True возвращает обе
    full = await build_todo_ics("localhost", include_done=True)
    assert full.count("BEGIN:VEVENT") == 2
    assert "✓ готово" in full  # выполненные помечены галочкой


@pytest.mark.asyncio
async def test_ics_empty_is_valid(db: aiosqlite.Connection) -> None:
    ics = await build_todo_ics("localhost")
    assert "BEGIN:VCALENDAR" in ics and "END:VCALENDAR" in ics
    assert ics.count("BEGIN:VEVENT") == 0
