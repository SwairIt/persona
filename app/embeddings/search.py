"""Semantic search via cosine similarity against all stored embeddings.

For up to ~100k screenshots brute-force on a single CPU is fast enough
(<200ms). When the corpus grows we'll move to an approximate index
(LanceDB / Qdrant), but for v0.1 simple wins.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import aiosqlite

from app.embeddings.model import embed_query
from app.embeddings.storage import decode_vector
from app.storage.time import iso, parse_iso


def _cosine(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two equal-length float vectors."""
    if len(a) != len(b):
        return 0.0
    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for x, y in zip(a, b, strict=True):
        dot += x * y
        norm_a += x * x
        norm_b += y * y
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (math.sqrt(norm_a) * math.sqrt(norm_b))


async def semantic_search(
    conn: aiosqlite.Connection,
    *,
    query: str,
    limit: int = 50,
    min_similarity: float = 0.15,
    since: datetime | None = None,
    until: datetime | None = None,
    app_name: str | None = None,
) -> list[dict[str, Any]]:
    """Run a semantic search across stored embeddings.

    Returns hits sorted by descending cosine similarity. Empty list if the
    embeddings model is not configured or no rows exist yet.
    """
    query = query.strip()
    if not query:
        return []

    query_vec = embed_query(query)

    # S4b — если доступен sqlite-vec, сначала сужаем до KNN-кандидатов (быстро,
    # в SQL), а cosine считаем уже только по ним. Без расширения / при ошибке —
    # vec_candidate_ids вернёт None и мы перебираем все строки как раньше.
    from app.embeddings.vec_store import vec_candidate_ids  # noqa: PLC0415

    candidate_ids = await vec_candidate_ids(query_vec, k=max(200, limit * 5))

    where: list[str] = []
    params: list[Any] = []
    if candidate_ids is not None:
        if not candidate_ids:
            return []  # vec0 есть, но индекс пуст для этого запроса
        placeholders = ",".join("?" * len(candidate_ids))
        where.append(f"s.id IN ({placeholders})")
        params.extend(candidate_ids)
    if since is not None:
        where.append("s.captured_at >= ?")
        params.append(iso(since))
    if until is not None:
        where.append("s.captured_at < ?")
        params.append(iso(until))
    if app_name is not None:
        where.append("s.app_name = ?")
        params.append(app_name)

    sql = (
        "SELECT s.id, s.captured_at, s.thumbnail_path, s.app_name, s.window_title, "
        "       s.ocr_text, e.vector "
        "FROM screenshot_embeddings e "
        "JOIN screenshots s ON s.id = e.screenshot_id"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)

    cursor = await conn.execute(sql, params)
    rows = await cursor.fetchall()

    scored: list[dict[str, Any]] = []
    for row in rows:
        vector = decode_vector(bytes(row["vector"]))
        score = _cosine(query_vec, vector)
        if score < min_similarity:
            continue
        ocr_text = row["ocr_text"] or ""
        scored.append(
            {
                "screenshot_id": int(row["id"]),
                "captured_at": parse_iso(str(row["captured_at"])),
                "thumbnail_path": row["thumbnail_path"],
                "app_name": row["app_name"],
                "window_title": row["window_title"],
                "snippet": _make_snippet(ocr_text, query),
                "similarity": round(score, 4),
            }
        )

    scored.sort(key=lambda h: h["similarity"], reverse=True)
    return scored[:limit]


def _make_snippet(text: str, query: str, max_len: int = 220) -> str:
    """Return a small context window around the first matching token, if any."""
    if not text:
        return ""
    needle = query.lower().split()[0] if query else ""
    haystack = text.lower()
    pos = haystack.find(needle) if needle else -1
    if pos < 0:
        return text[:max_len]
    start = max(0, pos - max_len // 2)
    return ("…" if start > 0 else "") + text[start : start + max_len]
