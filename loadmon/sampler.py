"""Снятие мгновенной нагрузки: CPU/RAM/GPU/VRAM + загруженные модели Ollama.

Всё best-effort: если nvidia-smi или Ollama недоступны — соответствующие поля
None/[], сервис не падает. Под Windows nvidia-smi кладётся драйвером в
System32, поэтому пробуем и PATH, и известные пути.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

import psutil

try:
    import httpx
except Exception:  # pragma: no cover
    httpx = None  # type: ignore

_OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")

_NVIDIA_SMI_CANDIDATES = [
    "nvidia-smi",
    r"C:\Windows\System32\nvidia-smi.exe",
    r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
]


def _resolve_nvidia_smi() -> str | None:
    found = shutil.which("nvidia-smi")
    if found:
        return found
    for cand in _NVIDIA_SMI_CANDIDATES:
        if os.path.exists(cand):
            return cand
    return None


_NVIDIA_SMI = _resolve_nvidia_smi()


def _gpus() -> list[dict[str, Any]]:
    """GPU через nvidia-smi (CSV). [] если нет NVIDIA/драйвера."""
    if not _NVIDIA_SMI:
        return []
    query = "utilization.gpu,memory.used,memory.total,temperature.gpu,name,power.draw"
    try:
        out = subprocess.run(
            [_NVIDIA_SMI, f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    gpus: list[dict[str, Any]] = []
    for idx, line in enumerate(out.stdout.strip().splitlines()):
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        def _num(x: str) -> float | None:
            try:
                return float(x)
            except (ValueError, TypeError):
                return None
        gpus.append({
            "index": idx,
            "util_pct": _num(parts[0]),
            "vram_used_mb": _num(parts[1]),
            "vram_total_mb": _num(parts[2]),
            "temp_c": _num(parts[3]),
            "name": parts[4],
            "power_w": _num(parts[5]) if len(parts) > 5 else None,
        })
    return gpus


def _ollama_models() -> list[dict[str, Any]]:
    """Загруженные сейчас модели Ollama (/api/ps). [] если Ollama не запущен."""
    if httpx is None:
        return []
    try:
        r = httpx.get(f"{_OLLAMA_URL}/api/ps", timeout=2.0)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception:
        return []
    models: list[dict[str, Any]] = []
    for m in (data.get("models") or []):
        size = m.get("size") or 0
        size_vram = m.get("size_vram") or 0
        models.append({
            "name": m.get("name") or m.get("model") or "?",
            "size_mb": round(size / 1048576) if size else None,
            "vram_mb": round(size_vram / 1048576) if size_vram else None,
            "on_gpu_pct": round(100 * size_vram / size) if size else None,
            "expires_at": m.get("expires_at"),
        })
    return models


def sample() -> dict[str, Any]:
    """Мгновенный снимок нагрузки. cpu_percent без интервала (с прошлого вызова)."""
    vm = psutil.virtual_memory()
    gpus = _gpus()
    primary = gpus[0] if gpus else {}
    return {
        "cpu_pct": psutil.cpu_percent(interval=None),
        "cpu_cores": psutil.cpu_count(logical=True),
        "ram_pct": vm.percent,
        "ram_used_mb": round(vm.used / 1048576),
        "ram_total_mb": round(vm.total / 1048576),
        "gpu_pct": primary.get("util_pct"),
        "vram_used_mb": primary.get("vram_used_mb"),
        "vram_total_mb": primary.get("vram_total_mb"),
        "gpu_temp_c": primary.get("temp_c"),
        "gpu_name": primary.get("name"),
        "gpus": gpus,
        "models": _ollama_models(),
        "nvidia_smi": bool(_NVIDIA_SMI),
        "ollama": bool(_ollama_models()) or None,
    }


def sample_for_db() -> dict[str, Any]:
    """Снимок в плоском виде для записи в SQLite (models — json-строкой)."""
    s = sample()
    return {
        "cpu_pct": s["cpu_pct"],
        "ram_pct": s["ram_pct"],
        "ram_used_mb": s["ram_used_mb"],
        "gpu_pct": s["gpu_pct"],
        "vram_used_mb": s["vram_used_mb"],
        "vram_total_mb": s["vram_total_mb"],
        "gpu_temp_c": s["gpu_temp_c"],
        "models_json": json.dumps([m["name"] for m in s["models"]], ensure_ascii=False),
    }


__all__ = ["sample", "sample_for_db"]
