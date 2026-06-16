"""Golden-eval recall как pytest: baseline-замер + регресс-гард.

Сеет фикс-корпус, мерит keyword-recall (recall_relevant) и hybrid_recall (на CI без
sqlite-vec он тихо падает в keyword — значения совпадут). Ассертит скромный baseline,
чтобы поймать ПОЛОМКУ recall, и печатает метрики для сравнения до/после изменений памяти.
"""

from __future__ import annotations

import aiosqlite
import pytest

from tests.eval.golden import measure, seed_corpus


@pytest.mark.asyncio
async def test_recall_golden_baseline(db: aiosqlite.Connection) -> None:
    from app.chat.sessions import recall_relevant

    await seed_corpus(db, user_id=1)
    res = await measure(recall_relevant, user_id=1, k=6)
    print("\n[golden-eval keyword] hit_rate=%.2f (%d/%d)" % (res["hit_rate"], res["hits"], res["n"]))
    for pq in res["per_query"]:
        if not pq["ok"]:
            print("  MISS:", pq["q"], "expected", pq["expect"], "found", pq["found"])
    # Baseline: keyword/FTS5 на этом корпусе должен брать минимум половину.
    # Это регресс-гард, а не цель — цель растить через hybrid+reranker (S1).
    assert res["hit_rate"] >= 0.5, f"recall регресс: {res}"


@pytest.mark.asyncio
async def test_recall_hybrid_fallback_not_worse(db: aiosqlite.Connection) -> None:
    """hybrid_recall без sqlite-vec обязан тихо откатываться на keyword (не хуже)."""
    from app.chat import hybrid_recall
    from app.chat.sessions import recall_relevant

    await seed_corpus(db, user_id=1)
    kw = await measure(recall_relevant, user_id=1, k=6)
    hy = await measure(hybrid_recall, user_id=1, k=6)
    print("\n[golden-eval] keyword=%.2f hybrid_fallback=%.2f" % (kw["hit_rate"], hy["hit_rate"]))
    assert hy["hit_rate"] >= kw["hit_rate"] - 0.001, "hybrid fallback хуже keyword — регресс"
