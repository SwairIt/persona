"""Tests for app.settings.config — typed env-driven configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.settings import get_settings


def test_settings_loads_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_CAPTURE_INTERVAL_SECONDS", "10.0")
    monkeypatch.setenv("PERSONA_THUMBNAIL_QUALITY", "75")
    monkeypatch.setenv("PERSONA_HOST", "0.0.0.0")
    monkeypatch.setenv("PERSONA_LOG_LEVEL", "DEBUG")
    get_settings.cache_clear()  # type: ignore[attr-defined]

    cfg = get_settings()
    assert cfg.capture_interval_seconds == 10.0
    assert cfg.thumbnail_quality == 75
    assert cfg.host == "0.0.0.0"
    assert cfg.log_level == "DEBUG"


def test_settings_rejects_bad_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_LOG_LEVEL", "VERBOSE")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    with pytest.raises(Exception):
        get_settings()


def test_settings_resolves_paths_absolute(tmp_path: Path) -> None:
    cfg = get_settings()
    assert cfg.data_dir.is_absolute()
    assert cfg.db_path.is_absolute()
    assert cfg.thumbnails_dir.is_absolute()


def test_settings_tesseract_path_empty_becomes_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_TESSERACT_PATH", "")
    get_settings.cache_clear()  # type: ignore[attr-defined]
    cfg = get_settings()
    assert cfg.tesseract_path is None
