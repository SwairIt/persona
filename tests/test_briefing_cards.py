"""Брифинг-карточки: хранение, фидбек, «избегай мимо», fallback (ROADMAP S3b)."""

from __future__ import annotations

import aiosqlite
import pytest

from app.briefing import (
    _fallback_cards,
    _recent_disliked_titles,
    dismiss_card,
    list_recent_cards,
    set_card_feedback,
    store_cards,
)


@pytest.mark.asyncio
async def test_store_and_list(db: aiosqlite.Connection) -> None:
    n = await store_cards(
        [
            {"icon": "✅", "title": "Закрыть PR", "body": "висит ревью"},
            {"icon": "📌", "title": "Созвон в 15:00", "body": ""},
        ],
        slot="morning",
    )
    assert n == 2
    cards = await list_recent_cards()
    titles = {c["title"] for c in cards}
    assert {"Закрыть PR", "Созвон в 15:00"} <= titles


@pytest.mark.asyncio
async def test_feedback_and_avoid(db: aiosqlite.Connection) -> None:
    await store_cards([{"icon": "•", "title": "ненужное", "body": "x"}], slot="morning")
    cid = (await list_recent_cards())[0]["id"]
    await set_card_feedback(cid, -1)
    assert "ненужное" in await _recent_disliked_titles()
    # 👍 переключает фидбек обратно в плюс
    await set_card_feedback(cid, 1)
    assert "ненужное" not in await _recent_disliked_titles()


@pytest.mark.asyncio
async def test_dismiss_hides(db: aiosqlite.Connection) -> None:
    await store_cards([{"icon": "•", "title": "скрой меня", "body": ""}], slot="morning")
    cid = (await list_recent_cards())[0]["id"]
    await dismiss_card(cid)
    assert all(c["id"] != cid for c in await list_recent_cards())


@pytest.mark.asyncio
async def test_store_replaces_same_slot_today(db: aiosqlite.Connection) -> None:
    await store_cards([{"icon": "•", "title": "старое", "body": ""}], slot="morning")
    await store_cards([{"icon": "•", "title": "новое", "body": ""}], slot="morning")
    titles = {c["title"] for c in await list_recent_cards()}
    assert "новое" in titles and "старое" not in titles  # пересоздан набор слота


def test_fallback_cards_splits_digest() -> None:
    digest = "- сделал А\n- осталось Б\n- короткий\n- запланировать В"
    cards = _fallback_cards(digest, "morning")
    titles = [c["title"] for c in cards]
    assert "сделал А" in titles[0]
    assert all(len(c["title"]) >= 6 for c in cards)  # «короткий» (<6 после strip) ок,
    # но мусорные пустышки отфильтрованы
