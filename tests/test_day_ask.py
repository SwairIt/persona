"""«Спросить про день» — POST /api/day/{date}/ask, контекст + LLM (BUILD_PLAN A3)."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from app.day_overview import today_iso
from app.storage.repository import insert_screenshot
from app.web.routes.day_overview_page import _day_context, answer_about_day


class _FakeClient:
    """Мини-заглушка LLM: запоминает промпт, возвращает фикс-ответ."""

    provider = "fake"

    def __init__(self) -> None:
        self.last_user = ""

    async def complete(self, req) -> str:  # noqa: ANN001
        self.last_user = req.user
        return "За день ты работал в TestApp."


@pytest.mark.asyncio
async def test_day_context_has_data(db: aiosqlite.Connection) -> None:
    day = today_iso()
    await insert_screenshot(db, captured_at=datetime.now(UTC), width=10, height=10,
                            phash="c1", app_name="TestApp", ocr_status="done")
    await db.execute("UPDATE screenshots SET ocr_text='секретный отчёт по продажам' WHERE phash='c1'")
    await db.execute("INSERT INTO day_tldr(day,tldr) VALUES(?,?)", (day, "продуктивный день"))
    await db.commit()

    ctx = await _day_context(day, user_id=1)
    assert day in ctx
    assert "Скриншотов: 1" in ctx
    assert "продуктивный день" in ctx
    assert "отчёт по продажам" in ctx  # OCR-сэмпл попал в контекст


@pytest.mark.asyncio
async def test_answer_uses_injected_client(db: aiosqlite.Connection) -> None:
    day = today_iso()
    await insert_screenshot(db, captured_at=datetime.now(UTC), width=10, height=10,
                            phash="c2", app_name="TestApp", ocr_status="done")
    await db.commit()

    fake = _FakeClient()
    res = await answer_about_day(day, user_id=1, question="над чем я работал?", client=fake)
    assert res["status"] == "ok"
    assert res["answer"] == "За день ты работал в TestApp."
    assert "над чем я работал?" in fake.last_user  # вопрос ушёл в промпт
    assert "Данные за день" in fake.last_user      # контекст подмешан


@pytest.mark.asyncio
async def test_empty_question_guard(db: aiosqlite.Connection) -> None:
    res = await answer_about_day(today_iso(), user_id=1, question="  ", client=_FakeClient())
    assert res["status"] == "empty"
