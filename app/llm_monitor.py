"""Мониторинг нагрузки LLM-модели (Ollama) — что загружено и сколько ест.

«Отслеживание нагрузки модели на ПК»: Persona-сервер и Ollama обычно на РАЗНЫХ
машинах (Ollama — на ПК пользователя, проброшен через devtunnel). Локальный
системный монитор (`app.system_metrics`) видит железо СЕРВЕРА, не ПК с моделью.
Зато сама Ollama честно отдаёт, что у неё загружено и сколько памяти/VRAM это
занимает, через `GET /api/ps` — это и есть реальная нагрузка модели на ПК.

`collect_llm_status()` дёргает у настроенного эндпоинта Ollama:
  - `/api/ps`   — РЕЗИДЕНТНЫЕ (загруженные) модели: размер, VRAM vs RAM, до когда
                  держится в памяти, на чём считает (GPU/CPU);
  - `/api/tags` — какие модели вообще установлены.
Всё best-effort: недоступность туннеля/Ollama → reachable=False, не падаем.
Эндпоинт берём ИЗ ТОЙ ЖЕ kv `byo_api_key_ollama`, что чат и эмбеддинги, чтобы
монитор смотрел на ту же машину, что реально обслуживает запросы.
"""

from __future__ import annotations

import os
import time
from typing import Any

from app.logging_setup import get_logger
from app.storage.db import get_connection
from app.storage.repository import get_kv

log = get_logger("persona.llm_monitor")

_DEFAULT_OLLAMA = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
_DEFAULT_EMBED_MODEL = "nomic-embed-text"
_TTL_SECONDS = 2.5  # короткий кэш: не дёргаем туннель чаще, чем раз в ~2.5с

_cache: dict[str, Any] | None = None
_cache_ts: float = 0.0


async def _endpoint() -> str:
    """Эндпоинт Ollama (kv ``byo_api_key_ollama``) — как у чата/эмбеддингов."""
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


async def _embed_model() -> str:
    try:
        async with get_connection() as conn:
            m = (await get_kv(conn, "embed_model") or "").strip()
        return m or _DEFAULT_EMBED_MODEL
    except Exception:  # noqa: BLE001
        return _DEFAULT_EMBED_MODEL


def _mb(n: Any) -> float | None:
    """Байты → МБ (округлённо), None при мусоре."""
    try:
        return round(float(n) / (1024 * 1024), 1)
    except (TypeError, ValueError):
        return None


def _running_model(m: dict[str, Any]) -> dict[str, Any]:
    """Нормализовать строку /api/ps в плоский вид для UI."""
    total = m.get("size")
    vram = m.get("size_vram")
    total_mb = _mb(total)
    vram_mb = _mb(vram)
    ram_mb = None
    if total_mb is not None and vram_mb is not None:
        ram_mb = round(max(0.0, total_mb - vram_mb), 1)
    # на чём считает: целиком GPU / целиком CPU / гибрид
    processor = "—"
    if total_mb:
        if vram_mb and vram_mb >= total_mb * 0.99:
            processor = "GPU"
        elif not vram_mb:
            processor = "CPU"
        else:
            pct = round(vram_mb / total_mb * 100) if total_mb else 0
            processor = f"GPU+CPU ({pct}% GPU)"
    details = m.get("details") or {}
    return {
        "name": m.get("name") or m.get("model") or "?",
        "size_mb": total_mb,
        "vram_mb": vram_mb,
        "ram_mb": ram_mb,
        "processor": processor,
        "expires_at": m.get("expires_at"),
        "params": details.get("parameter_size"),
        "quant": details.get("quantization_level"),
    }


async def collect_llm_status() -> dict[str, Any]:
    """Статус LLM: достижимость + загруженные/установленные модели. Best-effort,
    с коротким кэшем (TTL ~2.5с). Никогда не бросает исключений."""
    global _cache, _cache_ts
    now = time.monotonic()
    if _cache is not None and (now - _cache_ts) < _TTL_SECONDS:
        return _cache

    endpoint = await _endpoint()
    embed = await _embed_model()
    status: dict[str, Any] = {
        "endpoint": endpoint,
        "reachable": False,
        "embed_model": embed,
        "running": [],
        "installed": [],
        "running_count": 0,
        "installed_count": 0,
        "vram_total_mb": None,
        "error": None,
    }

    try:
        import httpx  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        status["error"] = "httpx недоступен"
        _cache, _cache_ts = status, now
        return status

    try:
        async with httpx.AsyncClient(timeout=4.0) as client:
            # резидентные модели = реальная нагрузка на ПК
            try:
                r = await client.get(f"{endpoint}/api/ps")
                if r.status_code == 200:
                    status["reachable"] = True
                    models = (r.json() or {}).get("models") or []
                    running = [_running_model(m) for m in models]
                    status["running"] = running
                    status["running_count"] = len(running)
                    vrams = [x["vram_mb"] for x in running if x.get("vram_mb")]
                    status["vram_total_mb"] = round(sum(vrams), 1) if vrams else None
            except Exception:  # noqa: BLE001 — /api/ps может не быть на старых Ollama
                pass
            # установленные модели (и заодно подтверждает достижимость)
            try:
                r = await client.get(f"{endpoint}/api/tags")
                if r.status_code == 200:
                    status["reachable"] = True
                    tags = (r.json() or {}).get("models") or []
                    names = [t.get("name") or t.get("model") for t in tags if (t.get("name") or t.get("model"))]
                    status["installed"] = names
                    status["installed_count"] = len(names)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 — туннель лёг / DNS / таймаут
        status["error"] = f"{type(exc).__name__}"

    if not status["reachable"] and not status["error"]:
        status["error"] = "Ollama недоступна (туннель не поднят?)"

    _cache, _cache_ts = status, now
    return status


__all__ = ["collect_llm_status"]
