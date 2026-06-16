"""Единый vec0-слой для эмбеддингов скриншотов (ROADMAP S4b), ОПЦИОНАЛЬНО.

Унифицирует поиск по скриншотам с памятью чатов (app/memory_vec.py): когда
установлен sqlite-vec, KNN-кандидаты достаются в SQL через vec0 (быстро, без
перебора всех BLOB в Python). Без расширения — ВСЁ тихо деградирует: функции —
no-op / None, а app/embeddings/search.py откатывается на полный перебор cosine.

Источник истины эмбеддингов остаётся ``screenshot_embeddings`` (BLOB). Таблица
``screenshot_vec`` (vec0, мигр.190) — ускоряющее ЗЕРКАЛО, наполняется
идемпотентным backfill по маркеру ``vec_screenshot_meta``. Точность не меняется:
vec0 отдаёт кандидатов, финальный скоринг — тем же cosine по исходным BLOB.
"""

from __future__ import annotations

import struct
from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection, sqlite_vec_available

log = get_logger("persona.embeddings.vec")


def vec_available() -> bool:
    """sqlite-vec доступен? (тонкая обёртка для единой точки правды)."""
    return sqlite_vec_available()


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


async def index_screenshot(
    screenshot_id: int,
    vector: list[float],
    *,
    captured_at: str | None = None,
    app_name: str | None = None,
) -> bool:
    """Зеркалировать эмбеддинг скриншота в vec0. No-op без sqlite-vec/вектора."""
    if not sqlite_vec_available() or not vector:
        return False
    try:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO screenshot_vec(screenshot_id, embedding) VALUES(?, ?)",
                (screenshot_id, _pack(vector)),
            )
            await conn.execute(
                "INSERT OR REPLACE INTO vec_screenshot_meta(screenshot_id, captured_at, app_name) "
                "VALUES(?,?,?)",
                (screenshot_id, captured_at, app_name),
            )
            await conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — vec-путь не должен ломать индексацию
        log.debug("vec_store.index_failed", error=str(exc))
        return False


async def backfill_vec(limit: int = 1000) -> int:
    """Докинуть в vec0 эмбеддинги из screenshot_embeddings, которых там ещё нет.

    Идемпотентно (маркер — vec_screenshot_meta). No-op без sqlite-vec. Возвращает
    число добавленных. Звать батчами в простое (как memory_vec.backfill_index).
    """
    if not sqlite_vec_available():
        return 0
    from app.embeddings.storage import decode_vector  # noqa: PLC0415

    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT e.screenshot_id, e.vector, s.captured_at, s.app_name "
                "FROM screenshot_embeddings e "
                "JOIN screenshots s ON s.id = e.screenshot_id "
                "LEFT JOIN vec_screenshot_meta v ON v.screenshot_id = e.screenshot_id "
                "WHERE v.screenshot_id IS NULL "
                "ORDER BY e.screenshot_id DESC LIMIT ?",
                (max(1, min(5000, int(limit))),),
            )
            rows = await cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.debug("vec_store.backfill_query_failed", error=str(exc))
        return 0
    done = 0
    for r in rows:
        try:
            vec = decode_vector(bytes(r["vector"]))
        except Exception:  # noqa: BLE001
            continue
        if await index_screenshot(
            int(r["screenshot_id"]), vec,
            captured_at=r["captured_at"], app_name=r["app_name"],
        ):
            done += 1
    if done:
        log.info("vec_store.backfill", indexed=done)
    return done


async def vec_candidate_ids(query_vec: list[float], k: int = 200) -> list[int] | None:
    """KNN по screenshot_vec → id кандидатов (порядок по близости).

    None — если sqlite-vec недоступен / таблицы нет / ошибка (вызывающий тогда
    делает полный перебор). [] — расширение есть, но индекс пуст.
    """
    if not sqlite_vec_available() or not query_vec:
        return None
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT screenshot_id FROM screenshot_vec "
                "WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                (_pack(query_vec), max(1, min(2000, int(k)))),
            )
            rows = await cur.fetchall()
        return [int(r["screenshot_id"]) for r in rows]
    except Exception as exc:  # noqa: BLE001 — нет vec0/таблицы/несовпадение dim
        log.debug("vec_store.knn_failed", error=str(exc))
        return None


__all__ = ["vec_available", "index_screenshot", "backfill_vec", "vec_candidate_ids"]
