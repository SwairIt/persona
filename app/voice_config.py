"""Конфиг голосовых движков (ROADMAP S4c) — серверный каркас.

ВАЖНО: сами движки (STT faster-whisper/whisper.cpp, Silero VAD, TTS
Piper/Kokoro/Silero/system, barge-in) работают НА УСТРОЙСТВЕ (Mac/Win-агент).
Сервер здесь только ХРАНИТ и ВАЛИДИРУЕТ предпочтения и отдаёт их агенту через
``GET /api/voice/config``; агент применяет их у себя. Реальная работа движков
проверяется на устройстве (NEEDS DEVICE) — этот модуль чисто про конфиг, и он
полностью детерминирован/тестируем.

Любое кривое/отсутствующее значение → безопасный дефолт (агент не должен падать
из-за мусора в kv). Числа клампятся в допустимый диапазон, перечисления —
ограничены известными движками.
"""

from __future__ import annotations

from typing import Any

from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

# Допустимые движки. Порядок в кортеже = приоритет по умолчанию.
STT_ENGINES = ("faster-whisper", "whisper.cpp", "server")
TTS_ENGINES = ("piper", "kokoro", "silero", "system")

_RATE_MIN, _RATE_MAX = 0.5, 2.0      # множитель скорости речи
_VAD_MIN, _VAD_MAX = 0.0, 1.0        # порог чувствительности VAD

DEFAULTS: dict[str, Any] = {
    "stt_engine": "faster-whisper",
    "vad_enabled": True,
    "vad_threshold": 0.5,
    "tts_engine": "piper",
    "tts_voice": "",          # имя голоса RU (зависит от движка)
    "tts_rate": 1.0,
    "barge_in": True,
}

# kv-ключи (плоско, как остальные настройки).
_KV = {
    "stt_engine": "voice_stt_engine",
    "vad_enabled": "voice_vad_enabled",
    "vad_threshold": "voice_vad_threshold",
    "tts_engine": "voice_tts_engine",
    "tts_voice": "voice_tts_voice",
    "tts_rate": "voice_tts_rate",
    "barge_in": "voice_barge_in",
}


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "on", "yes", "да"):
        return True
    if s in ("0", "false", "off", "no", "нет", ""):
        return False
    return default


def _as_float(value: Any, default: float, lo: float, hi: float) -> float:
    try:
        f = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, f))


def _as_enum(value: Any, allowed: tuple[str, ...], default: str) -> str:
    s = str(value or "").strip().lower()
    return s if s in allowed else default


def validate_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Привести произвольный ввод к валидному конфигу (чистая функция).

    Отсутствующее/кривое → дефолт; числа клампятся; движки ограничены known-list.
    """
    return {
        "stt_engine": _as_enum(raw.get("stt_engine"), STT_ENGINES, DEFAULTS["stt_engine"]),
        "vad_enabled": _as_bool(raw.get("vad_enabled"), DEFAULTS["vad_enabled"]),
        "vad_threshold": round(
            _as_float(raw.get("vad_threshold"), DEFAULTS["vad_threshold"], _VAD_MIN, _VAD_MAX), 3
        ),
        "tts_engine": _as_enum(raw.get("tts_engine"), TTS_ENGINES, DEFAULTS["tts_engine"]),
        "tts_voice": str(raw.get("tts_voice") or "").strip()[:80],
        "tts_rate": round(
            _as_float(raw.get("tts_rate"), DEFAULTS["tts_rate"], _RATE_MIN, _RATE_MAX), 2
        ),
        "barge_in": _as_bool(raw.get("barge_in"), DEFAULTS["barge_in"]),
    }


async def get_voice_config() -> dict[str, Any]:
    """Прочитать конфиг движков из kv (с дефолтами/валидацией)."""
    async with get_connection() as conn:
        raw = {key: await get_kv(conn, kv) for key, kv in _KV.items()}
    return validate_config(raw)


async def save_voice_config(values: dict[str, Any]) -> dict[str, Any]:
    """Провалидировать и записать конфиг движков. Возвращает сохранённое."""
    clean = validate_config(values)
    async with get_connection() as conn:
        for key, kv in _KV.items():
            v = clean[key]
            await set_kv(conn, kv, "1" if isinstance(v, bool) and v else
                         "0" if isinstance(v, bool) else str(v))
        await conn.commit()
    return clean


__all__ = [
    "STT_ENGINES", "TTS_ENGINES", "DEFAULTS",
    "validate_config", "get_voice_config", "save_voice_config",
]
