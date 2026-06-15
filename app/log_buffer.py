"""In-memory ring buffer of recent log lines — для live-логов в /root.

structlog-процессор ``ring_processor`` копирует каждое событие в кольцевой
буфер (deque) и best-effort публикует по SSE (type=log), затем ВОЗВРАЩАЕТ
event_dict БЕЗ ИЗМЕНЕНИЙ — это критично: он стоит перед ConsoleRenderer и не
должен ломать обычный вывод в stdout. Любая ошибка внутри глотается.

Ограничение: при ``--workers N`` у каждого воркера свой буфер и свои
SSE-подписчики. ``/root/logs/recent.json`` отдаёт логи того воркера, что
обслужил запрос; live-поток показывает события этого же воркера. Для личного
инструмента это приемлемо (лучше, чем ничего); durable system_log — позже.
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Any

_RING_MAX = 2000
_ring: deque[dict[str, Any]] = deque(maxlen=_RING_MAX)

_LEVELS = {"debug": 10, "info": 20, "warning": 30, "warn": 30, "error": 40, "critical": 50}

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
    """structlog-процессор: записать событие в кольцо + live-SSE. Fail-safe."""
    try:
        entry = _entry(event_dict, method_name)
        _ring.append(entry)
        _publish(entry)
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


__all__ = ["ring_processor", "get_recent", "buffer_size"]
