"""Очередь задач «Persona LLM Worker» (срез W-A) — серверное ядро.

Архитектура (убрать devtunnel): сервер кладёт задачи (chat/embed) в очередь в
БД, ПК-воркер делает ИСХОДЯЩИЕ long-poll-запросы, забирает задачу атомарно,
считает на локальной Ollama и шлёт ответ обратно по HTTP. Всё чистый HTTP —
дружит с FastPanel-прокси, без WebSocket.

Этот модуль — только работа с БД (таблицы llm_job / llm_job_chunk из миграции
203) + kv-настройки воркера (последний онлайн, модель, хэш токена). HTTP-обвязка
и провайдер живут в соседних слайсах.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone

from app.logging_setup import get_logger
from app.storage.db import get_connection, write_transaction
from app.storage.repository import get_kv, set_kv

log = get_logger("persona.llm.worker_queue")

# ── kv-ключи состояния воркера ────────────────────────────────────────────────
_KV_LAST_SEEN = "llm_worker_last_seen"   # ISO-время последнего пинга воркера
_KV_MODEL = "llm_worker_model"           # модель, которую анонсировал воркер
_KV_TOKEN_HASH = "llm_worker_token_hash"  # sha256 от плейнтекст-токена воркера

# Воркер считается онлайн, если пинговал не позже чем N секунд назад.
_ONLINE_WINDOW_SECONDS = 30.0


def _now_iso() -> str:
    """ISO-время в UTC (единый формат для kv/таймстампов)."""
    return datetime.now(timezone.utc).isoformat()


# ── задачи: enqueue / claim / chunk / finish / read / get ─────────────────────


async def enqueue_job(user_id: int, kind: str, model: str, payload: dict) -> int:
    """Поставить задачу в очередь, вернуть job_id.

    payload сериализуется в JSON. kind ожидается 'chat' | 'embed'.
    """
    payload_json = json.dumps(payload, ensure_ascii=False)
    async with write_transaction() as conn:
        cursor = await conn.execute(
            """
            INSERT INTO llm_job (user_id, kind, model, payload, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (user_id, kind, model, payload_json),
        )
        job_id = cursor.lastrowid
    if job_id is None:
        msg = "INSERT llm_job не вернул row id"
        raise RuntimeError(msg)
    return int(job_id)


async def claim_next(worker_id: str) -> dict | None:
    """АТОМАРНО забрать самую старую pending-задачу.

    UPDATE ... WHERE id=(SELECT ... pending ORDER BY id LIMIT 1) AND
    status='pending' — гарантия, что параллельный второй claim_next не заберёт
    ту же строку (проверяем changes()). Возвращает {id,kind,model,payload(dict)}
    или None, если pending-задач нет.
    """
    async with write_transaction() as conn:
        cursor = await conn.execute(
            """
            UPDATE llm_job
               SET status='streaming', worker_id=?, claimed_at=datetime('now')
             WHERE id = (
                 SELECT id FROM llm_job
                  WHERE status='pending'
                  ORDER BY id
                  LIMIT 1
             )
               AND status='pending'
            """,
            (worker_id,),
        )
        # changes() == 0 → задачу уже забрал кто-то другой (или очередь пуста).
        changes_cur = await conn.execute("SELECT changes() AS n")
        changes_row = await changes_cur.fetchone()
        if not changes_row or int(changes_row["n"]) == 0:
            return None
        # Достаём именно ту строку, которую сами только что застримили.
        row_cur = await conn.execute(
            """
            SELECT id, kind, model, payload
              FROM llm_job
             WHERE worker_id=? AND status='streaming'
             ORDER BY claimed_at DESC, id DESC
             LIMIT 1
            """,
            (worker_id,),
        )
        row = await row_cur.fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "kind": row["kind"],
        "model": row["model"],
        "payload": _loads_payload(row["payload"]),
    }


async def add_chunk(job_id: int, seq: int, content: str) -> None:
    """Дописать стрим-чанк ответа (для chat-задач)."""
    async with write_transaction() as conn:
        await conn.execute(
            "INSERT INTO llm_job_chunk (job_id, seq, content) VALUES (?, ?, ?)",
            (job_id, seq, content),
        )


async def finish_job(
    job_id: int, error: str | None = None, result: str | None = None
) -> None:
    """Завершить задачу: status='error' если есть error, иначе 'done'."""
    status = "error" if error else "done"
    async with write_transaction() as conn:
        await conn.execute(
            """
            UPDATE llm_job
               SET status=?, error=?, result=?, finished_at=datetime('now')
             WHERE id=?
            """,
            (status, error, result, job_id),
        )


async def read_chunks(job_id: int, after_seq: int) -> list[dict]:
    """Чанки задачи с seq > after_seq, по возрастанию seq."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT seq, content
              FROM llm_job_chunk
             WHERE job_id=? AND seq > ?
             ORDER BY seq
            """,
            (job_id, after_seq),
        )
        rows = await cursor.fetchall()
    return [{"seq": int(row["seq"]), "content": row["content"] or ""} for row in rows]


async def get_job(job_id: int) -> dict | None:
    """Полная строка задачи (включая status, error, result) или None."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT id, user_id, kind, model, payload, status, worker_id,
                   result, error, created_at, claimed_at, finished_at
              FROM llm_job
             WHERE id=?
            """,
            (job_id,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return {
        "id": int(row["id"]),
        "user_id": row["user_id"],
        "kind": row["kind"],
        "model": row["model"],
        "payload": _loads_payload(row["payload"]),
        "status": row["status"],
        "worker_id": row["worker_id"],
        "result": row["result"],
        "error": row["error"],
        "created_at": row["created_at"],
        "claimed_at": row["claimed_at"],
        "finished_at": row["finished_at"],
    }


def _loads_payload(raw: object) -> dict:
    """Распарсить payload-JSON в dict. Любой мусор → пустой dict (best-effort)."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ── состояние воркера: heartbeat / онлайн / статус ────────────────────────────


async def touch_worker(worker_id: str, model: str | None) -> None:
    """Отметить, что воркер на связи: обновить last_seen (+ модель, если есть)."""
    async with get_connection() as conn:
        await set_kv(conn, _KV_LAST_SEEN, _now_iso())
        if model:
            await set_kv(conn, _KV_MODEL, model)


async def worker_online() -> bool:
    """True, если воркер пинговал не позже чем _ONLINE_WINDOW_SECONDS назад."""
    async with get_connection() as conn:
        last_seen = await get_kv(conn, _KV_LAST_SEEN)
    return _is_fresh(last_seen)


async def worker_status() -> dict:
    """{online, model, last_seen} — для owner-статуса в UI."""
    async with get_connection() as conn:
        last_seen = await get_kv(conn, _KV_LAST_SEEN)
        model = await get_kv(conn, _KV_MODEL)
    return {
        "online": _is_fresh(last_seen),
        "model": model,
        "last_seen": last_seen,
    }


def _is_fresh(last_seen: str | None) -> bool:
    """True, если ISO-время last_seen внутри окна онлайн."""
    if not last_seen:
        return False
    try:
        seen = datetime.fromisoformat(last_seen)
    except ValueError:
        return False
    if seen.tzinfo is None:
        seen = seen.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - seen < timedelta(seconds=_ONLINE_WINDOW_SECONDS)


# ── токен воркера: rotate / validate ──────────────────────────────────────────


async def rotate_worker_token() -> str:
    """Сгенерить новый токен воркера, сохранить sha256 в kv, вернуть ПЛЕЙНТЕКСТ.

    Плейнтекст показывается ОДИН РАЗ (owner копирует в .env воркера). В БД лежит
    только sha256 — утечка БД не раскрывает действующий токен.
    """
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    async with get_connection() as conn:
        await set_kv(conn, _KV_TOKEN_HASH, digest)
    return token


async def validate_worker_token(token: str) -> bool:
    """True, если sha256(token) совпадает с сохранённым хэшем.

    Сравнение через hmac.compare_digest — защита от timing-атак.
    """
    if not token:
        return False
    async with get_connection() as conn:
        stored = await get_kv(conn, _KV_TOKEN_HASH)
    if not stored:
        return False
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return hmac.compare_digest(digest, stored)
