"""Конфиг голосовых движков — валидация + kv round-trip (ROADMAP S4c).

Только серверный каркас конфига (сами движки STT/TTS — на устройстве, NEEDS DEVICE).
"""

from __future__ import annotations

import aiosqlite
import pytest

from app.voice_config import (
    DEFAULTS,
    get_voice_config,
    save_voice_config,
    validate_config,
)


def test_defaults_on_empty() -> None:
    assert validate_config({}) == DEFAULTS


def test_enum_restriction_and_case() -> None:
    assert validate_config({"stt_engine": "bogus"})["stt_engine"] == DEFAULTS["stt_engine"]
    assert validate_config({"tts_engine": "KOKORO"})["tts_engine"] == "kokoro"


def test_numeric_clamping() -> None:
    assert validate_config({"tts_rate": "5.0"})["tts_rate"] == 2.0
    assert validate_config({"tts_rate": "0.1"})["tts_rate"] == 0.5
    assert validate_config({"vad_threshold": "-1"})["vad_threshold"] == 0.0
    assert validate_config({"vad_threshold": "2"})["vad_threshold"] == 1.0
    assert validate_config({"tts_rate": "1,5"})["tts_rate"] == 1.5  # запятая как десятичная


def test_bool_parsing() -> None:
    assert validate_config({"barge_in": "0"})["barge_in"] is False
    assert validate_config({"vad_enabled": "да"})["vad_enabled"] is True
    assert validate_config({"barge_in": "garbage"})["barge_in"] is DEFAULTS["barge_in"]


def test_garbage_numeric_falls_back() -> None:
    assert validate_config({"tts_rate": "abc"})["tts_rate"] == DEFAULTS["tts_rate"]


@pytest.mark.asyncio
async def test_save_and_get_roundtrip(db: aiosqlite.Connection) -> None:
    saved = await save_voice_config(
        {
            "stt_engine": "whisper.cpp",
            "vad_enabled": "1",
            "vad_threshold": "0.7",
            "tts_engine": "silero",
            "tts_voice": "ru_RU-irina",
            "tts_rate": "1.25",
            "barge_in": "0",
        }
    )
    assert saved["stt_engine"] == "whisper.cpp"
    assert saved["barge_in"] is False

    got = await get_voice_config()
    assert got["stt_engine"] == "whisper.cpp"
    assert got["vad_enabled"] is True
    assert got["vad_threshold"] == 0.7
    assert got["tts_engine"] == "silero"
    assert got["tts_voice"] == "ru_RU-irina"
    assert got["tts_rate"] == 1.25
    assert got["barge_in"] is False
