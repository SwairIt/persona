"""In-memory ring buffer of recent log lines — для live-логов в /root.

structlog-процессор ``ring_processor`` копирует каждое событие в кольцевой
буфер (deque) и best-effort публикует по SSE (type=log), затем ВОЗВРАЩАЕТ
event_dict БЕЗ ИЗМЕНЕНИЙ — это критично: он стоит перед ConsoleRenderer и не
должен ломать обычный вывод в stdout. Любая ошибка внутри глотается.

In-memory ``deque`` живёт в каждом воркере отдельно, поэтому для мгновенного
SSE-потока он остаётся источником истины. Параллельно (F6-06) значимые события
(level >= warning) best-effort складываются в durable-таблицу ``system_log`` —
её читает ``/root/logs/recent.json`` для сводной кросс-воркерной картины. Сама
запись в БД асинхронна (очередь + фоновый drain), троттлится по уровню и
ИСКЛЮЧАЕТ собственный логгер (``persona.log_buffer``), чтобы не было рекурсии
лог-записи-лога. Любой сбой БД глотается — логирование не должно падать/висеть.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any

_RING_MAX = 2000
_ring: deque[dict[str, Any]] = deque(maxlen=_RING_MAX)

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "warn": 30, "error": 40, "critical": 50}

# ── durable system_log (F6-06) ───────────────────────────────────────────────
# Порог записи в БД: только warning и выше, чтобы не бить по I/O.
_DB_LEVEL_FLOOR = 30
# Собственный логгер исключаем из durable-записи (защита от рекурсии).
_SELF_LOGGER = "persona.log_buffer"
# Очередь между sync-процессором и async-drain'ом. maxsize ограничивает память
# при всплеске: переполнение → молча роняем durable-запись (live-поток цел).
_DB_QUEUE_MAX = 1000
_db_queue: asyncio.Queue[dict[str, Any]] | None = None
_db_drain_task: asyncio.Task[None] | None = None
# Ретеншн: чистим старше N дней, не чаще раза в N секунд (без отдельного воркера).
_RETENTION_DAYS = 7
_RETENTION_EVERY_SEC = 3600.0
_last_retention_ts = 0.0

# ключи, которые НЕ выводим в лог-вьювере (возможные секреты)
_SECRET_HINT = ("token", "password", "secret", "authorization", "api_key", "apikey", "cookie")
_SKIP_KEYS = {"timestamp", "level", "event", "logger", "logger_name", "exc_info", "stack"}
_MAX_EXTRA = 6
_MAX_VAL_LEN = 200


def _extras(event_dict: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in event_dict.items():
        if k in _SKIP_KEYS:
            continue
        kl = str(k).lower()
        if any(h in kl for h in _SECRET_HINT):
            continue
        try:
            sval = str(v)
        except Exception:  # noqa: BLE001
            continue
        out[str(k)] = sval[:_MAX_VAL_LEN]
        if len(out) >= _MAX_EXTRA:
            break
    return out


def _entry(event_dict: dict[str, Any], method_name: str) -> dict[str, Any]:
    return {
        "ts": str(event_dict.get("timestamp") or ""),
        "level": str(event_dict.get("level") or method_name or "info").lower(),
        "logger": str(event_dict.get("logger") or event_dict.get("logger_name") or ""),
        "event": str(event_dict.get("event") or "")[:500],
        "extra": _extras(event_dict),
    }


def ring_processor(logger: Any, method_name: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """structlog-процессор: кольцо + live-SSE + durable system_log. Fail-safe."""
    try:
        entry = _entry(event_dict, method_name)
        _ring.append(entry)
        _publish(entry)
        _enqueue_durable(entry)
    except Exception:  # noqa: BLE001 — логирование НИКОГДА не должно падать
        pass
    return event_dict


def _publish(entry: dict[str, Any]) -> None:
    """Best-effort live-публикация по SSE, только если есть работающий loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # вне async-контекста (старт/воркер-тред) — только в кольцо
    try:
        from app.web.routes.live_sse import publish_log  # noqa: PLC0415 — избегаем цикла
        loop.create_task(publish_log(entry))
    except Exception:  # noqa: BLE001
        pass


def _enqueue_durable(entry: dict[str, Any]) -> None:
    """Best-effort поставить значимое событие в очередь на запись в system_log.

    Троттлинг: только level >= warning. Исключаем собственный логгер, чтобы не
    зациклиться (запись лога порождает свой лог). Без running loop (старт/тред)
    тихо пропускаем — durable-запись опциональна, live-deque уже сохранил.
    """
    if _LEVELS.get(entry.get("level", "info"), 20) < _DB_LEVEL_FLOOR:
        return
    if str(entry.get("logger") or "") == _SELF_LOGGER:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    queue = _ensure_db_worker(loop)
    if queue is None:
        return
    try:
        queue.put_nowait(entry)
    except asyncio.QueueFull:
        # Всплеск логов — durable-запись роняем молча, live-поток не страдает.
        pass


def _ensure_db_worker(loop: asyncio.AbstractEventLoop) -> asyncio.Queue[dict[str, Any]] | None:
    """Лениво создать очередь и фоновый drain-таск на текущем loop'е."""
    global _db_queue, _db_drain_task
    try:
        if _db_queue is None:
            _db_queue = asyncio.Queue(maxsize=_DB_QUEUE_MAX)
        if _db_drain_task is None or _db_drain_task.done():
            _db_drain_task = loop.create_task(_db_drain_loop(_db_queue))
        return _db_queue
    except Exception:  # noqa: BLE001
        return None


async def _db_drain_loop(queue: asyncio.Queue[dict[str, Any]]) -> None:
    """Фоновый цикл: батчами сливать события из очереди в system_log.

    Никаких исключений наружу: при любом сбое БД проглатываем и продолжаем —
    логирование важнее durable-копии. После каждого батча запускаем ленивый
    ретеншн (не чаще раза в час).
    """
    while True:
        try:
            first = await queue.get()
        except Exception:  # noqa: BLE001
            return
        batch = [first]
        # Подобрать всё, что уже накопилось (без ожидания) — амортизируем I/O.
        while len(batch) < 200:
            try:
                batch.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        try:
            await _write_batch(batch)
        except Exception:  # noqa: BLE001, S110 — БД-сбой не должен ронять drain
            pass
        try:
            await _maybe_retention()
        except Exception:  # noqa: BLE001, S110
            pass


async def _write_batch(batch: list[dict[str, Any]]) -> None:
    """Записать пачку событий одним INSERT'ом (best-effort, короткая запись)."""
    import os  # noqa: PLC0415

    worker_id = str(os.getpid())
    rows = []
    for e in batch:
        try:
            extra = e.get("extra") or {}
            extra_txt = json.dumps(extra, ensure_ascii=False) if extra else None
        except Exception:  # noqa: BLE001
            extra_txt = None
        rows.append(
            (
                str(e.get("ts") or "") or None,
                worker_id,
                str(e.get("level") or ""),
                str(e.get("logger") or ""),
                str(e.get("event") or "")[:500],
                extra_txt,
            )
        )
    if not rows:
        return
    from app.storage.db import write_transaction  # noqa: PLC0415 — избегаем цикла на импорте

    # ts: пустую строку оставляем NULL → сработает DEFAULT datetime('now').
    async with write_transaction() as conn:
        await conn.executemany(
            "INSERT INTO system_log (ts, worker_id, level, logger, event, extra) "
            "VALUES (COALESCE(?, datetime('now')), ?, ?, ?, ?, ?)",
            rows,
        )


async def _maybe_retention() -> None:
    """Ленивая чистка записей старше _RETENTION_DAYS — не чаще раза в час."""
    global _last_retention_ts
    now = time.monotonic()
    if now - _last_retention_ts < _RETENTION_EVERY_SEC:
        return
    _last_retention_ts = now
    from app.storage.db import write_transaction  # noqa: PLC0415

    async with write_transaction() as conn:
        await conn.execute(
            "DELETE FROM system_log WHERE ts < datetime('now', ?)",
            (f"-{int(_RETENTION_DAYS)} days",),
        )


def get_recent(limit: int = 300, level: str | None = None) -> list[dict[str, Any]]:
    """Последние записи (опционально с порогом уровня), старые → новые."""
    floor = _LEVELS.get((level or "").lower(), 0)
    items = list(_ring)
    if floor:
        items = [e for e in items if _LEVELS.get(e.get("level", "info"), 20) >= floor]
    limit = max(1, min(_RING_MAX, int(limit)))
    return items[-limit:]


def buffer_size() -> int:
    return len(_ring)


async def get_recent_durable(
    limit: int = 300,
    level: str | None = None,
    since: str | None = None,
) -> list[dict[str, Any]]:
    """Кросс-воркерные записи из system_log (агрегация по всем воркерам).

    Возвращает старые → новые (как in-memory get_recent), с разобранным
    ``extra`` обратно в dict. Фильтры: ``level`` (порог уровня) и ``since``
    (ISO-таймстемп — только записи строго новее). Best-effort: при любом сбое
    БД/отсутствии таблицы тихо отдаём пустой список.
    """
    floor = _LEVELS.get((level or "").lower(), 0)
    lim = max(1, min(_RING_MAX, int(limit)))
    where: list[str] = []
    params: list[Any] = []
    if floor:
        # Уровни, проходящие порог — список строк (worn/warn нормализованы в _LEVELS).
        allowed = [name for name, val in _LEVELS.items() if val >= floor]
        where.append("level IN (%s)" % ",".join("?" for _ in allowed))
        params.extend(allowed)
    if since:
        where.append("ts > ?")
        params.append(str(since))
    sql = "SELECT ts, worker_id, level, logger, event, extra FROM system_log"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # Берём последние lim по убыванию, затем разворачиваем в хронологию.
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(lim)
    out: list[dict[str, Any]] = []
    try:
        from app.storage.db import get_connection  # noqa: PLC0415

        async with get_connection() as conn:
            cur = await conn.execute(sql, params)
            db_rows = await cur.fetchall()
    except Exception:  # noqa: BLE001 — таблицы ещё нет / БД занята → пусто
        return []
    for r in reversed(db_rows):
        try:
            raw = r["extra"]
            extra = json.loads(raw) if raw else {}
            if not isinstance(extra, dict):
                extra = {}
        except Exception:  # noqa: BLE001
            extra = {}
        out.append(
            {
                "ts": str(r["ts"] or ""),
                "worker_id": str(r["worker_id"] or ""),
                "level": str(r["level"] or "info"),
                "logger": str(r["logger"] or ""),
                "event": str(r["event"] or ""),
                "extra": extra,
            }
        )
    return out


__all__ = ["ring_processor", "get_recent", "get_recent_durable", "buffer_size"]
