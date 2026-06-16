"""Аналитика за период (BUILD_PLAN B1)."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from app.analytics_overview import get_analytics
from app.storage.repository import insert_screenshot


@pytest.mark.asyncio
async def test_period_aggregates(db: aiosqlite.Connection) -> None:
    now = datetime.now(UTC)
    # 2 скрина сегодня (TestApp) + 1 звук + 1 ответ ИИ
    await insert_screenshot(db, captured_at=now, width=10, height=10, phash="p1",
                            app_name="TestApp", ocr_status="done")
    await insert_screenshot(db, captured_at=now, width=10, height=10, phash="p2",
                            app_name="TestApp", ocr_status="done")
    await db.execute(
        "INSERT INTO audio_segment(captured_at,ended_at,duration_seconds,codec,path,size_bytes) "
        "VALUES(?,?,?,?,?,?)", (now.isoformat(), now.isoformat(), 60.0, "opus", "x.opus", 100))
    await db.execute("INSERT INTO users(id,email,password_hash) VALUES(1,'a@b.c','x')")
    await db.execute("INSERT INTO chat_session(id,user_id,title) VALUES(3,1,'t')")
    await db.execute(
        "INSERT INTO chat_message(session_id,role,content,input_tokens,output_tokens) "
        "VALUES(3,'assistant','hi',10,5)")
    await db.commit()

    a = await get_analytics(days=7, user_id=1)
    assert a["days"] == 7
    assert len(a["series"]) == 7
    assert a["totals"]["screenshots"] == 2
    assert a["totals"]["audio_min"] == 1.0
    assert a["totals"]["ai_uses"] >= 1
    assert a["totals"]["tokens"] == 15
    assert a["totals"]["capture_days"] == 1
    assert any(x["app"] == "TestApp" for x in a["top_apps"])
    # сегодняшний день в серии последний
    assert a["series"][-1]["screenshots"] == 2


@pytest.mark.asyncio
async def test_days_clamped_and_empty_safe(db: aiosqlite.Connection) -> None:
    a = await get_analytics(days=999)  # некорректное → 30
    assert a["days"] == 30
    assert len(a["series"]) == 30
    assert a["totals"]["screenshots"] == 0
    assert a["totals"]["coverage_pct"] == 0
