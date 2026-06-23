"""Векторная / гибридная память (sqlite-vec + Ollama-эмбеддинги) — ОПЦИОНАЛЬНО.

Активируется ТОЛЬКО если: (1) установлен пакет sqlite-vec (`pip install sqlite-vec`),
(2) запущен Ollama с embed-моделью (`ollama pull nomic-embed-text`), (3) kv
``recall_mode`` = ``hybrid`` или ``vector``. Без любого из условий — ВСЁ тихо
откатывается на текущий keyword/FTS recall (recall_relevant), ничего не ломается.

hybrid_recall = FTS5 bm25 + векторный KNN, слитые через Reciprocal Rank Fusion
(RRF, k=60). ВНИМАНИЕ: bm25() в FTS5 ОТРИЦАТЕЛЬНЫЙ (меньше = релевантнее) →
ранжируем по rank ASC. Любая ошибка векторного пути → fallback на recall_relevant.
"""

from __future__ import annotations

import json
import os
import struct
from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection, sqlite_vec_available
from app.storage.repository import get_kv

log = get_logger("persona.memory_vec")

_DEFAULT_OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
_DEFAULT_EMBED_MODEL = "nomic-embed-text"
_RRF_K = 60


async def _embed_model() -> str:
    try:
        async with get_connection() as conn:
            m = (await get_kv(conn, "embed_model") or "").strip()
        return m or _DEFAULT_EMBED_MODEL
    except Exception:  # noqa: BLE001
        return _DEFAULT_EMBED_MODEL


async def _ollama_endpoint() -> str:
    """Тот же эндпоинт Ollama, что у чата (kv ``byo_api_key_ollama``) — чтобы
    эмбеддинги шли в ту же модель-машину (например, devtunnel на ПК), а не в
    localhost сервера, где Ollama нет. Гарантируем схему http(s)."""
    ep = ""
    try:
        async with get_connection() as conn:
            ep = (await get_kv(conn, "byo_api_key_ollama") or "").strip()
    except Exception:  # noqa: BLE001
        ep = ""
    ep = ep or _DEFAULT_OLLAMA
    if ep and not ep.startswith(("http://", "https://")):
        ep = "http://" + ep
    return ep.rstrip("/")


async def embed(text: str) -> list[float] | None:
    """Эмбеддинг текста через Ollama. None при любой проблеме (тихо)."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        import httpx  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return None
    model = await _embed_model()
    endpoint = await _ollama_endpoint()
    try:
        # timeout щедрый: первый embed после простоя грузит модель (cold start)
        # через туннель; keep_alive держит её тёплой, чтобы дальше было быстро.
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                f"{endpoint}/api/embeddings",
                json={"model": model, "prompt": text[:8000], "keep_alive": "30m"},
            )
            if r.status_code != 200:
                return None
            data = r.json()
        vec = data.get("embedding")
        if isinstance(vec, list) and vec:
            return [float(x) for x in vec]
    except Exception as exc:  # noqa: BLE001 — Ollama не запущен / нет модели
        log.debug("memory_vec.embed_failed", error=str(exc))
    return None


def _pack(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


async def index_message(message_id: int, user_id: int, content: str,
                        session_id: int | None = None, created_at: str | None = None) -> bool:
    """Проиндексировать одно сообщение (best-effort, no-op без sqlite-vec/эмбеддинга)."""
    if not sqlite_vec_available():
        return False
    vec = await embed(content)
    if not vec:
        return False
    try:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO chat_message_vec(message_id, embedding) VALUES(?, ?)",
                (message_id, _pack(vec)),
            )
            await conn.execute(
                "INSERT OR REPLACE INTO vec_message_meta(message_id, user_id, session_id, created_at) "
                "VALUES(?,?,?,?)",
                (message_id, user_id, session_id, created_at),
            )
            await conn.commit()
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("memory_vec.index_failed", error=str(exc))
        return False


async def _vector_hits(user_id: int, qvec: list[float], exclude_session_id: int | None,
                       k: int = 40) -> list[tuple[int, dict[str, Any]]]:
    """KNN по chat_message_vec → [(rank, row)] (rank с 1). [] при любой ошибке."""
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT v.message_id, m.content, m.role, m.created_at, s.title, m.session_id "
                "FROM chat_message_vec v "
                "JOIN vec_message_meta meta ON meta.message_id = v.message_id "
                "JOIN chat_message m ON m.id = v.message_id "
                "JOIN chat_session s ON s.id = m.session_id "
                "WHERE v.embedding MATCH ? AND k = ? AND meta.user_id = ? "
                "ORDER BY v.distance",
                (_pack(qvec), k, user_id),
            )
            rows = await cur.fetchall()
    except Exception as exc:  # noqa: BLE001 — нет vec0/таблицы/несовпадение dim
        log.debug("memory_vec.knn_failed", error=str(exc))
        return []
    out: list[tuple[int, dict[str, Any]]] = []
    rank = 0
    for r in rows:
        if exclude_session_id is not None and r["session_id"] == exclude_session_id:
            continue
        rank += 1
        out.append((rank, dict(r)))
    return out


async def _fts_hits(user_id: int, question: str, exclude_session_id: int | None,
                    k: int = 40) -> list[tuple[int, dict[str, Any]]]:
    """FTS5 bm25 top-k → [(rank, row)]. bm25 отрицательный → ORDER BY bm25 ASC."""
    from app.chat.sessions import _fts_expr  # noqa: PLC0415

    words = [w for w in question.lower().split() if len(w) >= 3][:10]
    expr = _fts_expr(words, op="OR")
    if not expr:
        return []
    try:
        # FTS5 MATCH/bm25 требуют ИМЯ таблицы, не алиас (иначе 'no such column').
        sql = (
            "SELECT m.id AS message_id, m.content, m.role, m.created_at, s.title, m.session_id "
            "FROM chat_message_fts "
            "JOIN chat_message m ON m.id = chat_message_fts.rowid "
            "JOIN chat_session s ON s.id = m.session_id "
            "WHERE chat_message_fts MATCH ? AND s.user_id = ? "
        )
        params: list[Any] = [expr, user_id]
        if exclude_session_id is not None:
            sql += "AND m.session_id != ? "
            params.append(exclude_session_id)
        sql += "ORDER BY bm25(chat_message_fts) LIMIT ?"
        params.append(k)
        async with get_connection() as conn:
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.debug("memory_vec.fts_failed", error=str(exc))
        return []
    return [(i + 1, dict(r)) for i, r in enumerate(rows)]


def _fmt(rows: list[dict[str, Any]], limit: int) -> str:
    out: list[str] = []
    seen: set[str] = set()
    for r in rows:
        txt = " ".join((r.get("content") or "").split())
        if len(txt) < 3:
            continue
        key = txt[:80]
        if key in seen:
            continue
        seen.add(key)
        who = "Ты" if r.get("role") == "user" else "Persona"
        title = (r.get("title") or "чат")
        out.append(f"• [{(r.get('created_at') or '')[:10]} · «{title[:24]}»] {who}: {txt[:280]}")
        if len(out) >= limit:
            break
    return "\n".join(out)


async def backfill_index(limit: int = 500, user_id: int | None = None) -> int:
    """Проиндексировать ещё не проиндексированные chat_message (идемпотентно).

    Маркер «уже проиндексировано» — наличие строки в vec_message_meta. No-op без
    sqlite-vec. Возвращает число проиндексированных. Звать батчами в простое.
    """
    if not sqlite_vec_available():
        return 0
    try:
        async with get_connection() as conn:
            sql = (
                "SELECT m.id, m.content, m.session_id, m.created_at, s.user_id "
                "FROM chat_message m JOIN chat_session s ON s.id = m.session_id "
                "LEFT JOIN vec_message_meta v ON v.message_id = m.id "
                "WHERE v.message_id IS NULL AND m.is_streaming = 0 "
                "AND length(m.content) >= 3 "
            )
            params: list[Any] = []
            if user_id is not None:
                sql += "AND s.user_id = ? "
                params.append(user_id)
            sql += "ORDER BY m.id DESC LIMIT ?"
            params.append(max(1, min(2000, int(limit))))
            cur = await conn.execute(sql, params)
            rows = await cur.fetchall()
    except Exception as exc:  # noqa: BLE001
        log.debug("memory_vec.backfill_query_failed", error=str(exc))
        return 0
    done = 0
    for r in rows:
        if await index_message(int(r["id"]), int(r["user_id"]), str(r["content"]),
                               session_id=r["session_id"], created_at=r["created_at"]):
            done += 1
    if done:
        log.info("memory_vec.backfill", indexed=done)
    return done


async def _rerank(question: str, rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Опц. cross-encoder реранк top-N (kv reranker_enabled). Без модели — как есть."""
    if len(rows) <= limit:
        return rows
    try:
        async with get_connection() as conn:
            on = (await get_kv(conn, "reranker_enabled") or "").strip() == "1"
        if not on:
            return rows
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: PLC0415

        model_name = "Xenova/bge-reranker-base"  # мультиязычный, CPU
        enc = TextCrossEncoder(model_name=model_name)
        docs = [r.get("content", "") for r in rows]
        scores = list(enc.rerank(question, docs))
        order = sorted(range(len(rows)), key=lambda i: -scores[i])
        return [rows[i] for i in order]
    except Exception as exc:  # noqa: BLE001 — реранк опционален, не ломаем recall
        log.debug("memory_vec.rerank_skipped", error=str(exc))
        return rows


def _norm_q(s: str) -> str:
    return " ".join((s or "").lower().split()).strip(" ?!.,—-")


def _is_echo(content: str, question: str) -> bool:
    """True, если результат — это по сути ПЕРЕСПРОС того же вопроса (эхо), а не
    содержательный ответ/факт. Такие эхо засоряют выдачу, когда один вопрос
    задавали много раз — и вытесняют реальный факт. Фильтруем их."""
    c, q = _norm_q(content), _norm_q(question)
    if not c or not q:
        return False
    if c == q:
        return True
    # короткий переспрос: один почти подстрока другого
    if len(c) <= len(q) + 14 and (q in c or c in q):
        return True
    return False


async def hybrid_recall(user_id: int, question: str,
                        exclude_session_id: int | None = None, limit: int = 6) -> str:
    """FTS5 bm25 + векторный KNN через RRF. Fallback на recall_relevant при любой
    проблеме / отсутствии sqlite-vec / пустом векторном результате."""
    from app.chat.sessions import recall_relevant  # noqa: PLC0415

    try:
        qvec = await embed(question) if sqlite_vec_available() else None
        vec_hits = await _vector_hits(user_id, qvec, exclude_session_id) if qvec else []
        fts_hits = await _fts_hits(user_id, question, exclude_session_id)
        if not vec_hits:
            # Нет вектора (sqlite-vec/Ollama/индекс отсутствуют) → обычный recall.
            return await recall_relevant(user_id, question, exclude_session_id, limit)
        # RRF-слияние: score = Σ 1/(k+rank) по каждому источнику.
        scores: dict[int, float] = {}
        rowmap: dict[int, dict[str, Any]] = {}
        for rank, r in vec_hits:
            mid = int(r["message_id"])
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (_RRF_K + rank)
            rowmap[mid] = r
        for rank, r in fts_hits:
            mid = int(r["message_id"])
            scores[mid] = scores.get(mid, 0.0) + 1.0 / (_RRF_K + rank)
            rowmap.setdefault(mid, r)
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        merged = [rowmap[mid] for mid, _ in ranked]
        # Выкинуть эхо-переспросы (когда вопрос задавали много раз дословно — они
        # вытесняют реальный факт). Если после фильтра пусто — оставляем как есть.
        filtered = [r for r in merged if not _is_echo(str(r.get("content") or ""), question)]
        merged = filtered or merged
        # Опц. финальный cross-encoder реранк top-N → точнее, чем RRF-порядок.
        merged = await _rerank(question, merged, limit)
        block = _fmt(merged, limit)
        return block or await recall_relevant(user_id, question, exclude_session_id, limit)
    except Exception as exc:  # noqa: BLE001 — векторный путь не должен ломать чат
        log.debug("memory_vec.hybrid_fallback", error=str(exc))
        return await recall_relevant(user_id, question, exclude_session_id, limit)


__all__ = ["backfill_index", "embed", "hybrid_recall", "index_message", "sqlite_vec_available"]
