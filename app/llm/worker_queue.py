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

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

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

# Очередь обслуживается лениво из ``claim_next``. Раньше каждый long-poll
# открывал BEGIN IMMEDIATE каждые 300 мс даже при пустой очереди. Теперь
# maintenance берёт write-lock не чаще раза в минуту, а пустой claim остаётся
# read-only.
_MAINTENANCE_INTERVAL_SECONDS = 60.0
_STALE_JOB_SECONDS = 15 * 60
_PENDING_JOB_SECONDS = 15 * 60
_TERMINAL_RETENTION_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_CHAT_MODEL = "qwen2.5:3b"
_DEFAULT_EMBED_MODEL = "nomic-embed-text"


class _MaintenanceState:
    def __init__(self) -> None:
        self.last_run = 0.0
        self.lock: asyncio.Lock | None = None


_maintenance = _MaintenanceState()

# WorkerLLMClient и HTTP-endpoints живут в одном server process. Event убирает
# частый DB-poll между чанками; timeout в клиенте остаётся fallback для
# multi-process deployments и рестартов процесса.
_job_update_events: dict[int, asyncio.Event] = {}
_pending_wakeup = asyncio.Event()


class WorkerJobStateError(RuntimeError):
    """Worker tried to mutate a job that is no longer streaming."""


async def worker_runtime_config() -> dict[str, str]:
    """Return the exact Ollama models that authenticated workers must provide."""
    async with get_connection() as conn:
        chat_model = (await get_kv(conn, "ollama_model") or "").strip()
        embed_model = (await get_kv(conn, "embed_model") or "").strip()
    return {
        "chat_model": chat_model or _DEFAULT_CHAT_MODEL,
        "embedding_model": embed_model or _DEFAULT_EMBED_MODEL,
    }


def _now_iso() -> str:
    """ISO-время в UTC (единый формат для kv/таймстампов)."""
    return datetime.now(timezone.utc).isoformat()


def _job_event(job_id: int) -> asyncio.Event:
    event = _job_update_events.get(job_id)
    if event is None:
        event = asyncio.Event()
        _job_update_events[job_id] = event
    return event


def _signal_job_update(job_id: int) -> None:
    event = _job_update_events.get(job_id)
    if event is not None:
        event.set()


async def wait_for_job_update(job_id: int, wait_seconds: float) -> bool:
    """Ждать изменения job без SQLite polling; False означает fallback timeout."""
    try:
        await asyncio.wait_for(
            _job_event(job_id).wait(), timeout=max(0.0, wait_seconds)
        )
    except TimeoutError:
        return False
    return True


async def wait_for_pending_job(wait_seconds: float) -> bool:
    """Ждать enqueue без захвата SQLite lock; timeout поддерживает multi-process."""
    try:
        await asyncio.wait_for(
            _pending_wakeup.wait(), timeout=max(0.0, wait_seconds)
        )
    except TimeoutError:
        return False
    return True


def forget_job_update(job_id: int) -> None:
    """Удалить process-local wakeup после завершения/отмены consumer."""
    _job_update_events.pop(job_id, None)


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
    normalized_job_id = int(job_id)
    _job_event(normalized_job_id)
    _pending_wakeup.set()
    return normalized_job_id


async def claim_next(worker_id: str) -> dict | None:
    """АТОМАРНО забрать самую старую pending-задачу.

    UPDATE ... WHERE id=(SELECT ... pending ORDER BY id LIMIT 1) AND
    status='pending' — гарантия, что параллельный второй claim_next не заберёт
    ту же строку (проверяем changes()). Возвращает {id,kind,model,payload(dict)}
    или None, если pending-задач нет.
    """
    await _maybe_maintain_jobs()

    # Пустая очередь — самый частый случай long-poll. Сначала делаем дешёвое
    # чтение, чтобы не брать глобальный SQLite write-lock без работы.
    # clear идёт ДО SELECT: enqueue после clear выставит Event, enqueue до clear
    # уже виден чтению из БД — пробуждение не теряется.
    _pending_wakeup.clear()
    async with get_connection() as conn:
        candidate_cur = await conn.execute(
            """
            SELECT id
              FROM llm_job
             WHERE status='pending'
             ORDER BY id
             LIMIT 1
            """
        )
        candidate = await candidate_cur.fetchone()
    if candidate is None:
        return None

    # UPDATE ... RETURNING возвращает ровно строку, захваченную этим вызовом.
    # Старый SELECT по worker_id мог вернуть другую streaming-job при двух
    # параллельных long-poll одного worker_id.
    async with write_transaction() as conn:
        cursor = await conn.execute(
            """
            UPDATE llm_job
               SET status='streaming', worker_id=?, claimed_at=datetime('now')
             WHERE id=?
               AND status='pending'
             RETURNING id, kind, model, payload
            """,
            (worker_id, int(candidate["id"])),
        )
        row = await cursor.fetchone()
    if row is None:
        # Другой claimant успел между read-only probe и UPDATE. Следующий
        # long-poll повторит попытку; чужую задачу не возвращаем.
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
        cursor = await conn.execute(
            """
            INSERT INTO llm_job_chunk (job_id, seq, content)
            SELECT ?, ?, ?
             WHERE EXISTS (
                 SELECT 1 FROM llm_job WHERE id=? AND status='streaming'
             )
            """,
            (job_id, seq, content, job_id),
        )
        if cursor.rowcount == 0:
            raise WorkerJobStateError(f"LLM job {job_id} is not streaming")
        # claimed_at играет роль lease heartbeat: пока воркер отдаёт чанки,
        # maintenance не пометит длинную генерацию зависшей.
        await conn.execute(
            "UPDATE llm_job SET claimed_at=datetime('now') WHERE id=?",
            (job_id,),
        )
        # Chunk delivery is a stronger heartbeat than an idle long-poll. Keep
        # the provider online while a slow local model is actively generating.
        await conn.execute(
            """
            INSERT INTO kv_settings(key, value, updated_at)
            VALUES(?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (_KV_LAST_SEEN, _now_iso()),
        )
    _signal_job_update(job_id)


async def finish_job(
    job_id: int, error: str | None = None, result: str | None = None
) -> None:
    """Завершить задачу: status='error' если есть error, иначе 'done'."""
    status = "error" if error else "done"
    async with write_transaction() as conn:
        cursor = await conn.execute(
            """
            UPDATE llm_job
               SET status=?, error=?, result=?, finished_at=datetime('now')
             WHERE id=? AND status='streaming'
             RETURNING worker_id
            """,
            (status, error, result, job_id),
        )
        row = await cursor.fetchone()
        if row is None:
            raise WorkerJobStateError(f"LLM job {job_id} is not streaming")
        # A tool-followup may be enqueued immediately after this commit.
        # Refresh last_seen here so it cannot reject the worker that has just
        # successfully completed the preceding generation.
        await conn.execute(
            """
            INSERT INTO kv_settings(key, value, updated_at)
            VALUES(?, ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (_KV_LAST_SEEN, _now_iso()),
        )
    _signal_job_update(job_id)


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
    return _job_from_row(row)


async def read_job_update(job_id: int, after_seq: int) -> tuple[list[dict], dict | None]:
    """Одним соединением прочитать новые чанки и состояние job.

    Event очищается *до* чтения: запись между snapshot и последующим wait
    снова выставит его, поэтому consumer не потеряет пробуждение.
    """
    _job_event(job_id).clear()
    async with get_connection() as conn:
        chunks_cur = await conn.execute(
            """
            SELECT seq, content
              FROM llm_job_chunk
             WHERE job_id=? AND seq > ?
             ORDER BY seq
            """,
            (job_id, after_seq),
        )
        rows = await chunks_cur.fetchall()
        job_cur = await conn.execute(
            """
            SELECT id, user_id, kind, model, payload, status, worker_id,
                   result, error, created_at, claimed_at, finished_at
              FROM llm_job
             WHERE id=?
            """,
            (job_id,),
        )
        job_row = await job_cur.fetchone()
    chunks = [
        {"seq": int(row["seq"]), "content": row["content"] or ""}
        for row in rows
    ]
    return chunks, _job_from_row(job_row)


def _job_from_row(row: Any | None) -> dict | None:
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


async def maintain_jobs(
    *,
    stale_after_seconds: int = _STALE_JOB_SECONDS,
    pending_after_seconds: int = _PENDING_JOB_SECONDS,
    retention_seconds: int = _TERMINAL_RETENTION_SECONDS,
) -> dict[str, int]:
    """Fail abandoned streaming jobs and delete expired terminal jobs/chunks."""
    stale_modifier = f"-{max(1, int(stale_after_seconds))} seconds"
    pending_modifier = f"-{max(1, int(pending_after_seconds))} seconds"
    retention_modifier = f"-{max(1, int(retention_seconds))} seconds"
    async with write_transaction() as conn:
        stale_cur = await conn.execute(
            """
            SELECT id
              FROM llm_job
             WHERE status='streaming'
               AND claimed_at < datetime('now', ?)
            """,
            (stale_modifier,),
        )
        stale_ids = [int(row["id"]) for row in await stale_cur.fetchall()]
        if stale_ids:
            await conn.execute(
                """
                UPDATE llm_job
                   SET status='error',
                       error='ПК-воркер потерял lease задачи',
                       finished_at=datetime('now')
                 WHERE status='streaming'
                   AND claimed_at < datetime('now', ?)
                """,
                (stale_modifier,),
            )

        pending_cur = await conn.execute(
            """
            SELECT id
              FROM llm_job
             WHERE status='pending'
               AND created_at < datetime('now', ?)
            """,
            (pending_modifier,),
        )
        pending_ids = [int(row["id"]) for row in await pending_cur.fetchall()]
        if pending_ids:
            await conn.execute(
                """
                UPDATE llm_job
                   SET status='error',
                       error='ПК-воркер не забрал задачу вовремя',
                       finished_at=datetime('now')
                 WHERE status='pending'
                   AND created_at < datetime('now', ?)
                """,
                (pending_modifier,),
            )

        expired_cur = await conn.execute(
            """
            SELECT id
              FROM llm_job
             WHERE status IN ('done', 'error')
               AND finished_at < datetime('now', ?)
            """,
            (retention_modifier,),
        )
        expired_ids = [int(row["id"]) for row in await expired_cur.fetchall()]
        if expired_ids:
            await conn.execute(
                """
                DELETE FROM llm_job_chunk
                 WHERE job_id IN (
                     SELECT id
                       FROM llm_job
                      WHERE status IN ('done', 'error')
                        AND finished_at < datetime('now', ?)
                 )
                """,
                (retention_modifier,),
            )
            await conn.execute(
                """
                DELETE FROM llm_job
                 WHERE status IN ('done', 'error')
                   AND finished_at < datetime('now', ?)
                """,
                (retention_modifier,),
            )

    for failed_id in (*stale_ids, *pending_ids):
        _signal_job_update(failed_id)
    for expired_id in expired_ids:
        forget_job_update(expired_id)
    return {
        "stale_failed": len(stale_ids),
        "pending_failed": len(pending_ids),
        "expired_deleted": len(expired_ids),
    }


async def _maybe_maintain_jobs() -> None:
    now = time.monotonic()
    if now - _maintenance.last_run < _MAINTENANCE_INTERVAL_SECONDS:
        return
    if _maintenance.lock is None:
        _maintenance.lock = asyncio.Lock()
    async with _maintenance.lock:
        now = time.monotonic()
        if now - _maintenance.last_run < _MAINTENANCE_INTERVAL_SECONDS:
            return
        await maintain_jobs()
        _maintenance.last_run = now


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
