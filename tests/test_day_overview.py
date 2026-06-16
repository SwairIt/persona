"""Агрегатор обзора дня (BUILD_PLAN A1)."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from app.day_overview import day_bounds_utc, get_day_overview, shift_day, today_iso
from app.storage.repository import insert_screenshot


def test_day_bounds_and_shift() -> None:
    since, until = day_bounds_utc("2026-06-16")
    assert since < until
    assert shift_day("2026-06-16", 1) == "2026-06-17"
    assert shift_day("2026-06-16", -1) == "2026-06-15"


@pytest.mark.asyncio
async def test_overview_aggregates(db: aiosqlite.Connection) -> None:
    day = today_iso()
    now = datetime.now(UTC)
    now_iso = now.isoformat()

    # 2 скриншота сегодня (app=TestApp), один OCR'нут
    await insert_screenshot(db, captured_at=now, width=10, height=10, phash="a1",
                            app_name="TestApp", ocr_status="done")
    await insert_screenshot(db, captured_at=now, width=10, height=10, phash="a2",
                            app_name="TestApp", ocr_status="pending")
    # звук 120с
    await db.execute(
        "INSERT INTO audio_segment(captured_at,ended_at,duration_seconds,codec,path,size_bytes) "
        "VALUES(?,?,?,?,?,?)", (now_iso, now_iso, 120.0, "opus", "a/b.opus", 1000))
    # чат: пользователь + сессия + ответ ассистента с токенами
    await db.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'a@b.c','x')")
    await db.execute("INSERT INTO chat_session(id,user_id,title) VALUES(7,1,'t')")
    await db.execute(
        "INSERT INTO chat_message(session_id,role,content,input_tokens,output_tokens) "
        "VALUES(7,'assistant','привет',100,40)")
    # TL;DR дня
    await db.execute("INSERT INTO day_tldr(day,tldr) VALUES(?,?)", (day, "тест дня"))
    await db.commit()

    ov = await get_day_overview(day, user_id=1)
    assert ov["day"] == day
    assert ov["screenshots"] == 2
    assert ov["ocr_done"] == 1
    assert ov["recorded"] is True
    assert ov["audio_seconds"] == 120
    assert ov["audio_minutes"] == 2.0
    assert ov["ai_replies"] == 1
    assert ov["input_tokens"] == 100
    assert ov["output_tokens"] == 40
    assert ov["total_tokens"] == 140
    assert ov["ai_uses"] >= 1
    assert ov["tldr"] == "тест дня"
    assert any(a["app"] == "TestApp" for a in ov["top_apps"])
    assert ov["prev_day"] == shift_day(day, -1)


@pytest.mark.asyncio
async def test_empty_day_is_safe(db: aiosqlite.Connection) -> None:
    ov = await get_day_overview("2020-01-01", user_id=1)
    assert ov["screenshots"] == 0
    assert ov["recorded"] is False
    assert ov["ai_uses"] == 0
    assert ov["tldr"] is None
