"""Pytest fixtures shared across all test modules."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import aiosqlite
import pytest
import pytest_asyncio

from app.settings import get_settings
from app.storage.db import init_database


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect Persona data paths to a per-test temp dir."""
    data_dir = tmp_path / "data"
    thumbs_dir = data_dir / "thumbnails"
    db_path = data_dir / "persona.db"

    monkeypatch.setenv("PERSONA_DATA_DIR", str(data_dir))
    monkeypatch.setenv("PERSONA_DB_PATH", str(db_path))
    monkeypatch.setenv("PERSONA_THUMBNAILS_DIR", str(thumbs_dir))
    monkeypatch.setenv("PERSONA_RETENTION_DAYS", "30")
    monkeypatch.setenv("PERSONA_CAPTURE_INTERVAL_SECONDS", "5")
    monkeypatch.setenv("PERSONA_OCR_ENABLED", "false")
    monkeypatch.delenv("PERSONA_TESSERACT_PATH", raising=False)

    get_settings.cache_clear()  # type: ignore[attr-defined]
    cfg = get_settings()
    cfg.ensure_directories()

    yield data_dir

    get_settings.cache_clear()  # type: ignore[attr-defined]


@pytest_asyncio.fixture
async def db() -> AsyncIterator[aiosqlite.Connection]:
    """Fresh database with schema applied."""
    await init_database()
    settings = get_settings()
    async with aiosqlite.connect(settings.db_path) as conn:
        conn.row_factory = aiosqlite.Row
        yield conn


@pytest.fixture
def is_windows() -> bool:
    return os.name == "nt"
