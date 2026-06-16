"""vec0-слой эмбеддингов скриншотов: мягкая деградация без sqlite-vec (ROADMAP S4b).

На dev-машине sqlite-vec НЕ установлен, поэтому здесь проверяем именно
fallback-контракт: функции — no-op/None, а semantic_search продолжает работать
полным перебором cosine (vec0-путь выключается прозрачно).
"""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite
import pytest

from app.embeddings.storage import upsert_embedding
from app.embeddings.vec_store import (
    backfill_vec,
    index_screenshot,
    vec_available,
    vec_candidate_ids,
)
from app.storage.repository import insert_screenshot, update_screenshot_ocr


@pytest.mark.asyncio
async def test_fallback_noops_without_sqlite_vec(db: aiosqlite.Connection) -> None:
    if vec_available():
        pytest.skip("sqlite-vec установлен — этот тест про fallback")
    assert await index_screenshot(1, [0.1] * 384) is False
    assert await backfill_vec() == 0
    assert await vec_candidate_ids([0.1] * 384) is None  # None → полный перебор


@pytest.mark.asyncio
async def test_meta_table_exists(db: aiosqlite.Connection) -> None:
    # Обычная таблица-маркер создаётся всегда (даже без расширения).
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_screenshot_meta'"
    )
    assert await cur.fetchone() is not None


@pytest.mark.asyncio
async def test_semantic_search_still_works_on_fallback(db: aiosqlite.Connection) -> None:
    if vec_available():
        pytest.skip("sqlite-vec установлен — проверяем именно перебор")
    from app.embeddings.search import semantic_search

    sid = await insert_screenshot(
        db, captured_at=datetime.now(timezone.utc), width=10, height=10,
        phash="abcdabcdabcdabcd", ocr_status="done",
    )
    await update_screenshot_ocr(db, sid, ocr_text="квартальный отчёт по продажам", ocr_status="done")
    # вектор-заглушка, совпадающий с запросом → cosine=1
    await upsert_embedding(
        db, screenshot_id=sid, vector=[1.0, 0.0, 0.0, 0.0],
        model="test-model", text="квартальный отчёт по продажам",
    )

    # monkeypatch embed_query, чтобы не грузить реальную модель
    import app.embeddings.search as search_mod

    orig = search_mod.embed_query
    search_mod.embed_query = lambda q: [1.0, 0.0, 0.0, 0.0]
    try:
        hits = await semantic_search(db, query="отчёт", limit=5, min_similarity=0.1)
    finally:
        search_mod.embed_query = orig

    assert any(h["screenshot_id"] == sid for h in hits)
    assert hits[0]["similarity"] >= 0.9  # точный cosine сохранён
