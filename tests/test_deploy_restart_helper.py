"""Хелпер деплоя: «успех» обязан означать «сайт отдаёт нужную версию».

Прошлый отказ был тихим: скрипт рапортовал «перезапущено», а сайт продолжал
отдавать старый код. Здесь зафиксировано, что так больше нельзя — проверка
считается пройденной, только если И ``/healthz`` (версия процесса на порту),
И все ``?v=`` в HTML сошлись с ``app/__init__.py``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ops import deploy_restart as dr

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

BASE = "http://127.0.0.1:8000"


def _responses(
    monkeypatch: pytest.MonkeyPatch,
    *,
    healthz: str | None,
    landing: str | None,
) -> None:
    """Подменить HTTP: None = не отвечает."""

    def fake_get(url: str, timeout: float = 10.0) -> tuple[int, str] | None:
        if url.endswith("/healthz"):
            return None if healthz is None else (200, json.dumps({"status": "ok", "version": healthz}))
        if url.endswith("/landing"):
            return None if landing is None else (200, landing)
        return None

    monkeypatch.setattr(dr, "_get", fake_get)


def _html(*versions: str) -> str:
    tags = "".join(f'<link href="/static/app.css?v={v}">' for v in versions)
    return f"<html><head>{tags}</head><body>ok</body></html>"


# --- verify ----------------------------------------------------------------
def test_verify_passes_only_when_both_signals_match(monkeypatch: pytest.MonkeyPatch) -> None:
    _responses(monkeypatch, healthz="2.34.0", landing=_html("2.34.0", "2.34.0"))

    ok, detail = dr.verify(BASE, "2.34.0")

    assert ok is True
    assert "2.34.0" in detail


def test_verify_fails_when_the_process_serves_an_older_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ровно прошлый тихий отказ: порт жив, код старый."""
    _responses(monkeypatch, healthz="2.33.1", landing=_html("2.33.1"))

    ok, detail = dr.verify(BASE, "2.34.0")

    assert ok is False
    assert "2.33.1" in detail and "2.34.0" in detail


def test_verify_fails_when_html_still_carries_a_stale_asset_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/healthz свежий, но в HTML остался старый ?v= — тоже провал."""
    _responses(monkeypatch, healthz="2.34.0", landing=_html("2.34.0", "2.33.1"))

    ok, detail = dr.verify(BASE, "2.34.0")

    assert ok is False
    assert "2.33.1" in detail


def test_verify_fails_when_the_server_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    _responses(monkeypatch, healthz=None, landing=None)

    ok, detail = dr.verify(BASE, "2.34.0")

    assert ok is False
    assert "не отвечает" in detail


def test_verify_fails_when_landing_has_no_version_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _responses(monkeypatch, healthz="2.34.0", landing="<html>без ассетов</html>")

    ok, _detail = dr.verify(BASE, "2.34.0")

    assert ok is False


def test_landing_is_not_fetched_until_healthz_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Дешёвая проба первая — не долбим тяжёлую страницу каждые 3 секунды."""
    fetched: list[str] = []

    def fake_get(url: str, timeout: float = 10.0) -> tuple[int, str] | None:
        fetched.append(url)
        return (200, json.dumps({"version": "2.33.1"})) if url.endswith("/healthz") else None

    monkeypatch.setattr(dr, "_get", fake_get)

    dr.verify(BASE, "2.34.0")

    assert all(url.endswith("/healthz") for url in fetched)


# --- версия из рабочей копии ----------------------------------------------
def test_repo_version_reads_app_init(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text('__version__ = "3.0.1"\n', encoding="utf-8")

    assert dr.repo_version(tmp_path) == "3.0.1"


# --- каталог данных должен совпасть с тем, куда смотрит watchdog -----------
def test_data_dir_follows_the_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PERSONA_DATA_DIR", str(tmp_path / "elsewhere"))

    assert dr.data_dir(tmp_path) == tmp_path / "elsewhere"


def test_data_dir_falls_back_to_dotenv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("PERSONA_DATA_DIR", raising=False)
    (tmp_path / ".env").write_text(
        "FOO=1\nPERSONA_DATA_DIR=C:/Users/Someone/.persona\n", encoding="utf-8"
    )

    assert dr.data_dir(tmp_path).as_posix().lower().endswith("someone/.persona")


def test_data_dir_agrees_with_the_watchdogs_own_resolver() -> None:
    """Маркер обязан лечь ровно туда, куда смотрит watchdog, — иначе тишина.

    Сравниваем не константы (watchdog вычисляет свою на импорте, до подмены
    окружения в conftest), а сами резолверы на одном и том же окружении.
    """
    import os

    from ops import persona_watchdog as wd

    expected = os.path.normpath(wd._detect_data_dir(wd._detect_home()))

    assert str(dr.data_dir()) == expected
