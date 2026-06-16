"""Инструмент schedule_reminder в tool-loop (ROADMAP S3b-2)."""

from __future__ import annotations

from datetime import date, timedelta

import aiosqlite
import pytest

from app.mcp.builtin_tools import call_tool
from app.storage.reminders import list_for_day


@pytest.mark.asyncio
async def test_tool_creates_reminder(db: aiosqlite.Connection) -> None:
    out = await call_tool("schedule_reminder", {"text": "напомни завтра оплатить хостинг"})
    assert out.startswith("[ok]")
    rows = await list_for_day(db, day=date.today() + timedelta(days=1))
    assert any("хостинг" in r["body"] for r in rows)


@pytest.mark.asyncio
async def test_tool_alias_remind(db: aiosqlite.Connection) -> None:
    # «remind» — алиас → тот же инструмент.
    out = await call_tool("remind", {"text": "сегодня купить хлеб"})
    assert out.startswith("[ok]")
    rows = await list_for_day(db, day=date.today())
    assert any("хлеб" in r["body"] for r in rows)


@pytest.mark.asyncio
async def test_tool_explicit_body_when(db: aiosqlite.Connection) -> None:
    out = await call_tool("schedule_reminder", {"body": "созвон с командой", "when": "через 3 дня"})
    assert out.startswith("[ok]")
    rows = await list_for_day(db, day=date.today() + timedelta(days=3))
    assert any("созвон" in r["body"] for r in rows)


@pytest.mark.asyncio
async def test_tool_needs_input(db: aiosqlite.Connection) -> None:
    out = await call_tool("schedule_reminder", {})
    assert out.startswith("[error]")
