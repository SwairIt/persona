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
    # Проба достижимости почтового транспорта (app/mail_transport.reachable)
    # открывает НАСТОЯЩИЙ TCP-сокет. Набор в сеть не ходит: без этого выключателя
    # каждый тест, дошедший до delivery_status(), стучался бы в релей из .env
    # разработчика. Тесты, которым проба нужна, включают её сами.
    monkeypatch.setenv("PERSONA_MAIL_PROBE", "0")
    monkeypatch.delenv("PERSONA_TESSERACT_PATH", raising=False)

    get_settings.cache_clear()  # type: ignore[attr-defined]
    cfg = get_settings()
    cfg.ensure_directories()

    yield data_dir

    get_settings.cache_clear()  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _reset_in_process_security_state() -> Iterator[None]:
    """Wipe the process-global counters the security layer keeps between tests.

    ``app.web.rate_limit``, the per-account lockout, the throttle/CSRF/CSP kv
    caches and the email-verification cache all live in module-level dicts by
    design (no Redis, no table). Each test gets a fresh *database* but the same
    interpreter, so without this a member who made N chat calls in one test
    starts the next one already near the ceiling — a false failure that looks
    like a product bug.

    The synchronous-reader caches in ``app.web.templates_engine`` / ``app.i18n``
    are cleared here for the same reason, and because the theme one is a
    **ContextVar that survives the test boundary**: a value left by an earlier
    module makes ``get_theme()`` answer without ever reading the database, so
    the wrong user's theme leaks forward. Nine test modules had each grown
    their own copy of that reset and a tenth (the streaming-scope test added
    tonight) forgot it — green alone, red in the full run. Doing it centrally
    removes the footgun instead of documenting it ten more times.
    """
    from app import i18n, member_crypto
    from app.auth import account_state, lockout, proxies, verification
    from app.web import rate_limit, templates_engine
    from app.web.middleware import csrf, security_headers, throttle

    def _wipe() -> None:
        # Ключи шифрования участников кэшируются в процессе (мастер-ключ +
        # развёрнутые DEK'и). Каждый тест получает СВОЙ PERSONA_DATA_DIR и свою
        # базу, поэтому без сброса второй тест шифровал бы данные ключом
        # первого — и в новой базе не появилось бы строки user_encryption_key.
        member_crypto.reset_cache()
        templates_engine._kv_value_cache.clear()
        templates_engine._user_kv_value_cache.clear()
        templates_engine.invalidate_theme_cache()
        i18n.invalidate_language_cache()
        # Both the auth routes' per-IP windows and the throttle's per-user
        # windows live in this one dict; clearing it covers both.
        rate_limit._EVENTS.clear()
        throttle.reset_state()  # budgets cache + its own counters, explicitly
        lockout.reset_all()
        account_state.reset_probe()
        proxies.reset_cache()
        verification.reset_cache()
        csrf.reset_cache()
        security_headers.reset_cache()

    _wipe()
    yield
    _wipe()


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
