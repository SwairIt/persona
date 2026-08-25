"""Watchdog-сторона рестарта по запросу — без планировщика и без повышения.

Проверяется то, ради чего механизм и делался:

* невалидный маркер НЕ приводит к рестарту (и не остаётся лежать);
* валидный — приводит ровно к одному рестарту;
* рестарт, который «сработал», но сервер отдаёт СТАРУЮ версию, считается
  **провалом** и орёт в лог — а не рапортует успех;
* uvicorn, который не удалось убить (ровно случай неповышенного шелла против
  процесса сессии 0), тоже провал с прямым указанием на повышение.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ops import persona_watchdog as wd
from ops import restart_request as rr

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _fake_repo(tmp_path: Path, version: str = "2.34.0") -> Path:
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "__init__.py").write_text(
        f'"""x."""\n\n__version__ = "{version}"\n', encoding="utf-8"
    )
    return repo


def _wire(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str = "2.34.0") -> Path:
    """Подменить пути watchdog'а на tmp и заглушить всё, что трогает систему."""
    repo = _fake_repo(tmp_path, version)
    # conftest уже создаёт tmp_path/"data" под изолированный PERSONA_DATA_DIR
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    monkeypatch.setattr(wd, "REPO", str(repo))
    monkeypatch.setattr(wd, "PERSONA_DIR", str(data))
    monkeypatch.setattr(wd, "WLOG", str(data / "watchdog.log"))
    monkeypatch.setattr(wd, "STATE_FILE", str(data / "watchdog_state"))
    monkeypatch.setattr(wd.time, "sleep", lambda _seconds: None)
    return repo


def _log_text(tmp_path: Path) -> str:
    path = tmp_path / "data" / "watchdog.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


# --- маршрутизация запроса -------------------------------------------------
def test_no_marker_means_no_restart(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _wire(monkeypatch, tmp_path)
    calls: list[str | None] = []
    monkeypatch.setattr(wd, "_restart_now", lambda expected: calls.append(expected) or (True, ""))

    assert wd._handle_restart_request() is False
    assert calls == []


def test_valid_marker_triggers_exactly_one_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _wire(monkeypatch, tmp_path, "2.34.0")
    calls: list[str | None] = []
    monkeypatch.setattr(
        wd, "_restart_now", lambda expected: (calls.append(expected), (True, "serving 2.34.0"))[1]
    )
    rr.write_request(wd.PERSONA_DIR, str(repo), "2.34.0")

    assert wd._handle_restart_request() is True
    # второй тик — маркера уже нет
    assert wd._handle_restart_request() is False

    assert calls == ["2.34.0"]
    assert "RESTART-REQUEST OK" in _log_text(tmp_path)
    assert rr.read_result(wd.PERSONA_DIR)["status"] == "ok"


def test_invalid_marker_is_ignored_and_discarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _wire(monkeypatch, tmp_path)
    restarted: list[object] = []
    monkeypatch.setattr(wd, "_restart_now", restarted.append)
    rr.write_request(wd.PERSONA_DIR, "D:/some/other/project", "2.34.0")

    assert wd._handle_restart_request() is False

    assert restarted == []
    assert not rr.marker_path(wd.PERSONA_DIR).exists()
    assert "RESTART-REQUEST IGNORED" in _log_text(tmp_path)
    assert rr.read_result(wd.PERSONA_DIR)["status"] == "ignored"
    assert str(repo)  # repo не при чём — маркер был для чужого каталога


def test_marker_asking_for_an_older_version_restarts_to_what_is_on_disk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _wire(monkeypatch, tmp_path, "2.34.0")
    calls: list[str | None] = []
    monkeypatch.setattr(
        wd, "_restart_now", lambda expected: (calls.append(expected), (True, "ok"))[1]
    )
    rr.write_request(wd.PERSONA_DIR, str(repo), "2.33.1")

    wd._handle_restart_request()

    assert calls == ["2.34.0"]


# --- провал рестарта слышно ------------------------------------------------
def test_failed_restart_is_logged_loudly_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(wd, "_restart_now", lambda _expected: (False, "serving 2.33.1"))
    rr.write_request(wd.PERSONA_DIR, str(repo), "2.34.0")

    assert wd._handle_restart_request() is True

    log = _log_text(tmp_path)
    assert "RESTART-REQUEST FAILED" in log
    assert "serving 2.33.1" in log
    result = rr.read_result(wd.PERSONA_DIR)
    assert result["status"] == "failed"


def test_restart_that_raises_is_reported_as_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _wire(monkeypatch, tmp_path)

    def _boom(_expected: str | None) -> tuple[bool, str]:
        raise RuntimeError("powershell gone")

    monkeypatch.setattr(wd, "_restart_now", _boom)
    rr.write_request(wd.PERSONA_DIR, str(repo), "2.34.0")

    wd._handle_restart_request()

    assert "RESTART-REQUEST FAILED" in _log_text(tmp_path)
    assert rr.read_result(wd.PERSONA_DIR)["status"] == "failed"


# --- _restart_now: честная проверка версии ---------------------------------
def test_restart_now_fails_when_the_old_process_survives_the_kill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Ровно провал неповышенного шелла против uvicorn'а сессии 0."""
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(wd, "_server_pids", lambda: [230028])
    monkeypatch.setattr(wd, "_kill_existing", lambda: None)
    started: list[int] = []
    monkeypatch.setattr(wd, "_start", lambda: started.append(1))

    ok, detail = wd._restart_now("2.34.0")

    assert ok is False
    assert "230028" in detail
    assert "ELEVATED" in detail
    assert started == []  # второй uvicorn поверх живого НЕ поднимаем


def test_restart_now_fails_when_the_served_version_stays_old(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(wd, "_server_pids", lambda: [])
    monkeypatch.setattr(wd, "_kill_existing", lambda: None)
    monkeypatch.setattr(wd, "_start", lambda: None)
    monkeypatch.setattr(wd, "_served_version", lambda: "2.33.1")
    monkeypatch.setattr(wd, "_RESTART_VERIFY_SECONDS", 0.01)

    ok, detail = wd._restart_now("2.34.0")

    assert ok is False
    assert "2.33.1" in detail and "2.34.0" in detail


def test_restart_now_reports_the_pid_turnover_as_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Рестарт на ТУ ЖЕ версию версией не докажешь — доказательство в смене PID'ов."""
    _wire(monkeypatch, tmp_path)
    snapshots = iter([[193512], [], [234552]])  # до kill / после kill / после старта
    monkeypatch.setattr(wd, "_server_pids", lambda: next(snapshots, []))
    monkeypatch.setattr(wd, "_kill_existing", lambda: None)
    monkeypatch.setattr(wd, "_start", lambda: 131836)
    monkeypatch.setattr(wd, "_served_version", lambda: "2.34.0")

    ok, detail = wd._restart_now("2.34.0")

    assert ok is True
    assert "234552" in detail  # новый
    assert "193512" in detail  # и старый, для сравнения


def test_restart_now_succeeds_only_on_the_expected_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _wire(monkeypatch, tmp_path)
    monkeypatch.setattr(wd, "_server_pids", lambda: [])
    monkeypatch.setattr(wd, "_kill_existing", lambda: None)
    monkeypatch.setattr(wd, "_start", lambda: None)
    answers = iter([None, "2.33.1", "2.34.0"])
    monkeypatch.setattr(wd, "_served_version", lambda: next(answers, "2.34.0"))

    ok, detail = wd._restart_now("2.34.0")

    assert ok is True
    assert "2.34.0" in detail


# --- версия из рабочей копии ----------------------------------------------
def test_repo_version_reads_app_init(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _wire(monkeypatch, tmp_path, "9.1.2")

    assert wd._repo_version() == "9.1.2"


def test_repo_version_is_none_when_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(wd, "REPO", str(tmp_path / "does-not-exist"))

    assert wd._repo_version() is None
