"""Сбор метрик ПК через ``psutil`` — best-effort, никогда не падает.

Один публичный вход — :func:`collect_system_metrics`, возвращает плоский
dict со снимком состояния хоста (CPU/память/диски/сеть/процессы/батарея/…).
КАЖДЫЙ psutil-вызов обёрнут в try/except и при сбое отдаёт ``None``/``0``,
поэтому внешние ошибки psutil (экзотическое железо, отсутствие сенсоров,
урезанные права) НЕ роняют приложение — тихий fallback.

Кроссплатформенность:
* Windows / macOS / Linux — базовые метрики (CPU, память, диски, сеть,
  процессы) работают везде.
* ``load_avg`` — есть на Unix (``os.getloadavg``); на Windows бросает →
  отдаём ``None``.
* ``temperatures`` — ``psutil.sensors_temperatures`` есть в основном на
  Linux; на Windows/macOS обычно пусто → ``{}``.
* ``battery`` — ноутбуки; на десктопах/серверах ``None``.
* ``gpu`` — best-effort через ``nvidia-smi`` (если стоит драйвер NVIDIA),
  иначе ``None``.

Кэш: результат :func:`collect_system_metrics` кэшируется на ~1.5 c
(TTL по ``time.monotonic``), чтобы частые опросы (UI-поллинг) не дёргали
psutil каждый запрос. Ring-buffer на 60 последних снимков
(``cpu_percent``/``memory_percent``/``ts``) — для мини-графиков истории.

Первый вызов ``cpu_percent(interval=None)`` всегда вернёт ``0.0``
(прогрев — psutil меряет дельту между вызовами) — это нормально.
"""

from __future__ import annotations

import collections
import subprocess
import time
from typing import Any

try:  # psutil заявлен в зависимостях, но импорт всё равно защищаем
    import psutil
except Exception:  # pragma: no cover - psutil обязан быть, но не роняемся
    psutil = None  # type: ignore[assignment]


# --- Кэш снимка (TTL ~1.5 c по монотонным часам) -------------------------
_CACHE_TTL = 1.5  # секунды
_cache: dict[str, Any] | None = None
_cache_ts: float = 0.0

# --- Ring-buffer последних снимков (cpu/mem/ts) --------------------------
_HISTORY_MAXLEN = 60
_history: collections.deque[dict[str, Any]] = collections.deque(maxlen=_HISTORY_MAXLEN)


def _safe(fn, default=None):
    """Вызвать ``fn`` и вернуть результат либо ``default`` при любой ошибке."""
    try:
        return fn()
    except Exception:
        return default


def _cpu_percent() -> float:
    """Неблокирующий общий процент CPU (первый вызов = 0.0, прогрев)."""
    return _safe(lambda: float(psutil.cpu_percent(interval=None)), 0.0) or 0.0


def _per_core() -> list[float]:
    """Процент загрузки по каждому ядру (неблокирующий)."""
    return _safe(
        lambda: [float(x) for x in psutil.cpu_percent(interval=None, percpu=True)],
        [],
    ) or []


def _cpu_count() -> int | None:
    """Число логических ядер."""
    return _safe(lambda: int(psutil.cpu_count() or 0)) or None


def _memory() -> dict[str, Any]:
    """Снимок ОЗУ + процент swap. Каждое поле best-effort."""
    out: dict[str, Any] = {
        "used": 0,
        "available": 0,
        "percent": 0.0,
        "total": 0,
        "swap_percent": 0.0,
    }
    vm = _safe(psutil.virtual_memory)
    if vm is not None:
        out["used"] = _safe(lambda: int(vm.used), 0)
        out["available"] = _safe(lambda: int(vm.available), 0)
        out["percent"] = _safe(lambda: float(vm.percent), 0.0)
        out["total"] = _safe(lambda: int(vm.total), 0)
    sm = _safe(psutil.swap_memory)
    if sm is not None:
        out["swap_percent"] = _safe(lambda: float(sm.percent), 0.0)
    return out


def _disk_usage() -> list[dict[str, Any]]:
    """Использование по каждому смонтированному разделу (каждый в try)."""
    out: list[dict[str, Any]] = []
    partitions = _safe(lambda: psutil.disk_partitions(all=False), []) or []
    for part in partitions:
        mount = _safe(lambda p=part: p.mountpoint)
        if not mount:
            continue
        usage = _safe(lambda m=mount: psutil.disk_usage(m))
        if usage is None:
            # Раздел без доступа (пустой CD-привод и т.п.) — пропускаем
            continue
        out.append(
            {
                "mount": mount,
                "percent": _safe(lambda u=usage: float(u.percent), 0.0),
                "free_gb": _safe(lambda u=usage: round(u.free / (1024**3), 2), 0.0),
                "total_gb": _safe(lambda u=usage: round(u.total / (1024**3), 2), 0.0),
            }
        )
    return out


def _disk_io() -> dict[str, int]:
    """Суммарный дисковый I/O с момента загрузки системы."""
    out = {"read_bytes": 0, "write_bytes": 0}
    io = _safe(psutil.disk_io_counters)
    if io is not None:
        out["read_bytes"] = _safe(lambda: int(io.read_bytes), 0)
        out["write_bytes"] = _safe(lambda: int(io.write_bytes), 0)
    return out


def _net_io() -> dict[str, int]:
    """Суммарный сетевой трафик с момента загрузки системы."""
    out = {"bytes_sent": 0, "bytes_recv": 0}
    io = _safe(psutil.net_io_counters)
    if io is not None:
        out["bytes_sent"] = _safe(lambda: int(io.bytes_sent), 0)
        out["bytes_recv"] = _safe(lambda: int(io.bytes_recv), 0)
    return out


def _processes_count() -> int:
    """Количество запущенных процессов."""
    return _safe(lambda: len(psutil.pids()), 0) or 0


def _top_processes() -> dict[str, list[dict[str, Any]]]:
    """Top-5 процессов по CPU и top-5 по памяти.

    ``process_iter`` запрашиваем ТОЛЬКО нужные атрибуты — так psutil не
    дёргает дорогие поля и не падает на процессах без доступа.
    """
    procs: list[dict[str, Any]] = []
    try:
        for proc in psutil.process_iter(
            ["pid", "name", "cpu_percent", "memory_percent"]
        ):
            info = proc.info  # type: ignore[attr-defined]
            procs.append(
                {
                    "pid": info.get("pid"),
                    "name": info.get("name") or "?",
                    "cpu_percent": float(info.get("cpu_percent") or 0.0),
                    "memory_percent": float(info.get("memory_percent") or 0.0),
                }
            )
    except Exception:
        return {"by_cpu": [], "by_memory": []}

    by_cpu = sorted(procs, key=lambda p: p["cpu_percent"], reverse=True)[:5]
    by_memory = sorted(procs, key=lambda p: p["memory_percent"], reverse=True)[:5]
    return {"by_cpu": by_cpu, "by_memory": by_memory}


def _battery() -> dict[str, Any] | None:
    """Состояние батареи или ``None`` (десктоп/сервер/нет сенсора)."""
    battery = _safe(psutil.sensors_battery)
    if battery is None:
        return None
    secsleft = _safe(lambda: int(battery.secsleft))
    # psutil отдаёт спец-константы (UNLIMITED/UNKNOWN) — отрицательные → None
    if secsleft is not None and secsleft < 0:
        secsleft = None
    return {
        "percent": _safe(lambda: float(battery.percent)),
        "plugged": _safe(lambda: bool(battery.power_plugged)),
        "secsleft": secsleft,
    }


def _uptime_seconds() -> int | None:
    """Аптайм в секундах (now − boot_time)."""
    boot = _safe(psutil.boot_time)
    if boot is None:
        return None
    return _safe(lambda: int(time.time() - boot))


def _load_avg() -> list[float] | None:
    """Load average (1/5/15 мин). На Windows бросает → ``None``."""
    import os

    getloadavg = getattr(os, "getloadavg", None)
    if getloadavg is None:
        return None
    return _safe(lambda: [round(float(x), 2) for x in getloadavg()])


def _temperatures() -> dict[str, Any]:
    """Температурные сенсоры best-effort (в основном Linux), иначе ``{}``."""
    fn = getattr(psutil, "sensors_temperatures", None)
    if fn is None:
        return {}
    raw = _safe(fn, {}) or {}
    out: dict[str, Any] = {}
    for name, entries in raw.items():
        readings: list[dict[str, Any]] = []
        for entry in entries:
            readings.append(
                {
                    "label": _safe(lambda e=entry: e.label) or "",
                    "current": _safe(lambda e=entry: float(e.current)),
                    "high": _safe(lambda e=entry: float(e.high) if e.high else None),
                }
            )
        out[name] = readings
    return out


def _gpu() -> dict[str, Any] | None:
    """GPU NVIDIA через ``nvidia-smi`` (best-effort) или ``None``.

    Если ``nvidia-smi`` не установлен / нет NVIDIA-карты / таймаут —
    тихо возвращаем ``None`` (не ошибка, просто нет данных).
    """
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=utilization.gpu,memory.used,memory.total",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return None
    # Первая GPU; формат: "util, mem_used, mem_total"
    parts = [p.strip() for p in line[0].split(",")]
    if len(parts) < 3:
        return None
    try:
        return {
            "util": float(parts[0]),
            "mem_used": float(parts[1]),
            "mem_total": float(parts[2]),
        }
    except (ValueError, TypeError):
        return None


def push_sample(cpu_percent: float, memory_percent: float) -> None:
    """Добавить снимок (cpu/mem/ts) в ring-buffer истории."""
    _history.append(
        {
            "cpu_percent": float(cpu_percent),
            "memory_percent": float(memory_percent),
            "ts": time.time(),
        }
    )


def get_history() -> list[dict[str, Any]]:
    """Вернуть список последних снимков истории (старые → новые)."""
    return list(_history)


def collect_system_metrics() -> dict[str, Any]:
    """Собрать полный снимок метрик ПК (кэш TTL ~1.5 c).

    Возвращает плоский dict со следующими ключами:

    * ``cpu_percent``       — float, общий % CPU (первый вызов 0.0, прогрев)
    * ``per_core``          — list[float], % по каждому логическому ядру
    * ``cpu_count``         — int | None, число логических ядер
    * ``memory``            — dict: ``used``, ``available``, ``percent``,
                              ``total`` (байты/проценты) + ``swap_percent``
    * ``disk_usage``        — list[dict]: ``mount``, ``percent``,
                              ``free_gb``, ``total_gb`` (по разделам)
    * ``disk_io``           — dict: ``read_bytes``, ``write_bytes``
    * ``net_io``            — dict: ``bytes_sent``, ``bytes_recv``
    * ``processes_count``   — int, число процессов
    * ``top_processes``     — dict: ``by_cpu`` / ``by_memory`` — по 5 dict-ов
                              ``{pid, name, cpu_percent, memory_percent}``
    * ``battery``           — dict ``{percent, plugged, secsleft}`` | None
    * ``uptime_seconds``    — int | None
    * ``load_avg``          — list[float] (1/5/15) | None (None на Windows)
    * ``temperatures``      — dict сенсоров (часто пусто вне Linux)
    * ``gpu``               — dict ``{util, mem_used, mem_total}`` | None
    * ``ts``                — float, время сбора (``time.time()``)

    При недоступном psutil вернёт безопасный «пустой» снимок без ошибок.
    Дополнительно пушит ``cpu_percent``/``memory.percent`` в ring-buffer
    истории (см. :func:`get_history`).
    """
    global _cache, _cache_ts

    now = time.monotonic()
    if _cache is not None and (now - _cache_ts) < _CACHE_TTL:
        return _cache

    if psutil is None:
        # psutil недоступен — отдаём безопасный пустой снимок
        snapshot: dict[str, Any] = {
            "cpu_percent": 0.0,
            "per_core": [],
            "cpu_count": None,
            "memory": {
                "used": 0,
                "available": 0,
                "percent": 0.0,
                "total": 0,
                "swap_percent": 0.0,
            },
            "disk_usage": [],
            "disk_io": {"read_bytes": 0, "write_bytes": 0},
            "net_io": {"bytes_sent": 0, "bytes_recv": 0},
            "processes_count": 0,
            "top_processes": {"by_cpu": [], "by_memory": []},
            "battery": None,
            "uptime_seconds": None,
            "load_avg": None,
            "temperatures": {},
            "gpu": None,
            "ts": time.time(),
        }
        _cache = snapshot
        _cache_ts = now
        return snapshot

    memory = _memory()
    snapshot = {
        "cpu_percent": _cpu_percent(),
        "per_core": _per_core(),
        "cpu_count": _cpu_count(),
        "memory": memory,
        "disk_usage": _disk_usage(),
        "disk_io": _disk_io(),
        "net_io": _net_io(),
        "processes_count": _processes_count(),
        "top_processes": _top_processes(),
        "battery": _battery(),
        "uptime_seconds": _uptime_seconds(),
        "load_avg": _load_avg(),
        "temperatures": _temperatures(),
        "gpu": _gpu(),
        "ts": time.time(),
    }

    # Пушим в историю для мини-графиков (не роняемся при кривых данных)
    _safe(
        lambda: push_sample(
            float(snapshot["cpu_percent"]),
            float(memory.get("percent", 0.0)),
        )
    )

    _cache = snapshot
    _cache_ts = now
    return snapshot


__all__ = ["collect_system_metrics", "push_sample", "get_history"]
