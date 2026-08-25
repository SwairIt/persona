"""Контракт маркера «перезапусти сайт» — без единого повышения прав.

Механизм описан в ``ops/restart_request.py``: неповышенный деплой кладёт
маркер, повышенный watchdog его валидирует и потребляет. Здесь проверяется
ровно то, что делает этот обмен безопасным:

* маркер вне единственного разрешённого пути не существует для watchdog'а;
* протухший / из будущего / чужого репо / битый маркер НЕ вызывает рестарт;
* потреблённый маркер не срабатывает второй раз (в т.ч. если файл вернут);
* любой исход выбрасывает маркер — вечного «жуём каждую минуту» не бывает.

Тесты гоняют чистую логику на tmp-каталогах: ни планировщика, ни сети, ни
реального uvicorn.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING

from ops import restart_request as rr

if TYPE_CHECKING:
    from pathlib import Path

REPO = "C:/some/repo/Persona"
NOW = 1_700_000_000.0


def _write(data_dir: Path, **overrides: object) -> dict:
    payload = rr.build_payload(REPO, "9.9.9", now=NOW)
    payload.update(overrides)
    rr._atomic_write_json(rr.marker_path(data_dir), payload)
    os.utime(rr.marker_path(data_dir), (NOW, NOW))
    return payload


# --- happy path ------------------------------------------------------------
def test_valid_marker_is_accepted_and_carries_the_payload(tmp_path: Path) -> None:
    payload = rr.write_request(tmp_path, REPO, "2.34.0", now=NOW)

    decision = rr.consume(tmp_path, REPO, now=NOW + 5)

    assert decision.accepted, decision.reason
    assert decision.payload is not None
    assert decision.payload["version"] == "2.34.0"
    assert decision.nonce == payload["nonce"]


def test_repo_comparison_ignores_separators_and_case(tmp_path: Path) -> None:
    rr.write_request(tmp_path, r"C:\some\repo\Persona", "2.34.0", now=NOW)

    decision = rr.consume(tmp_path, "c:/some/repo/persona", now=NOW + 1)

    assert decision.accepted, decision.reason


# --- расположение ----------------------------------------------------------
def test_marker_in_another_directory_does_not_exist_for_the_watchdog(
    tmp_path: Path,
) -> None:
    elsewhere = tmp_path / "elsewhere"
    watched = tmp_path / "watched"
    watched.mkdir()
    rr.write_request(elsewhere, REPO, "2.34.0", now=NOW)

    decision = rr.consume(watched, REPO, now=NOW + 1)

    assert decision.status == "none"
    # чужой файл не тронут — watchdog вообще не знает о таком пути
    assert rr.marker_path(elsewhere).exists()


def test_wrongly_named_file_in_the_data_dir_is_not_a_request(tmp_path: Path) -> None:
    (tmp_path / "restart.request.txt").write_text("{}", encoding="utf-8")

    assert rr.consume(tmp_path, REPO, now=NOW).status == "none"


# --- возраст ---------------------------------------------------------------
def test_stale_request_expires_instead_of_restarting_forever(tmp_path: Path) -> None:
    rr.write_request(tmp_path, REPO, "2.34.0", now=NOW)
    os.utime(rr.marker_path(tmp_path), (NOW, NOW))

    late = NOW + rr.MAX_AGE_SECONDS + 30
    decision = rr.consume(tmp_path, REPO, now=late)

    assert not decision.accepted
    assert "stale" in decision.reason
    # и — главное — маркер выброшен, а не оставлен рестартить сайт вечно
    assert not rr.marker_path(tmp_path).exists()


def test_fresh_body_on_an_old_file_is_still_stale(tmp_path: Path) -> None:
    """mtime — независимая проверка: подложить свежий ``requested_at`` мало."""
    _write(tmp_path, requested_at=NOW + 3600)
    os.utime(rr.marker_path(tmp_path), (NOW, NOW))

    decision = rr.consume(tmp_path, REPO, now=NOW + 3601)

    assert not decision.accepted
    assert "stale marker file" in decision.reason


def test_request_from_the_future_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, requested_at=NOW + 6000)

    decision = rr.consume(tmp_path, REPO, now=NOW)

    assert not decision.accepted
    assert "future" in decision.reason


# --- содержимое ------------------------------------------------------------
def test_marker_for_another_repo_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, repo="D:/www/QuadroFlow")

    decision = rr.consume(tmp_path, REPO, now=NOW + 1)

    assert not decision.accepted
    assert "repo mismatch" in decision.reason


def test_unknown_kind_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, kind="something-else/1")

    assert not rr.consume(tmp_path, REPO, now=NOW + 1).accepted


def test_empty_and_garbage_files_are_rejected(tmp_path: Path) -> None:
    for content in ("", "   ", "not json at all", "[1,2,3]", '"just a string"'):
        rr.marker_path(tmp_path).write_text(content, encoding="utf-8")
        os.utime(rr.marker_path(tmp_path), (NOW, NOW))

        decision = rr.consume(tmp_path, REPO, now=NOW + 1)

        assert not decision.accepted, content
        assert not rr.marker_path(tmp_path).exists()


def test_missing_fields_are_rejected(tmp_path: Path) -> None:
    for missing in ("repo", "version", "nonce", "requested_at"):
        payload = rr.build_payload(REPO, "2.34.0", now=NOW)
        payload.pop(missing)
        rr._atomic_write_json(rr.marker_path(tmp_path), payload)
        os.utime(rr.marker_path(tmp_path), (NOW, NOW))

        decision = rr.consume(tmp_path, REPO, now=NOW + 1)

        assert not decision.accepted, missing


def test_bad_version_and_nonce_are_rejected(tmp_path: Path) -> None:
    _write(tmp_path, version="2.34.0 & calc.exe")
    assert not rr.consume(tmp_path, REPO, now=NOW + 1).accepted

    _write(tmp_path, nonce="not-hex")
    assert not rr.consume(tmp_path, REPO, now=NOW + 1).accepted


def test_oversized_marker_is_not_even_parsed(tmp_path: Path) -> None:
    payload = rr.build_payload(REPO, "2.34.0", now=NOW)
    payload["padding"] = "x" * (rr.MAX_MARKER_BYTES * 2)
    rr.marker_path(tmp_path).write_text(json.dumps(payload), encoding="utf-8")
    os.utime(rr.marker_path(tmp_path), (NOW, NOW))

    decision = rr.consume(tmp_path, REPO, now=NOW + 1)

    assert not decision.accepted
    assert "oversized" in decision.reason
    assert not rr.marker_path(tmp_path).exists()


# --- однократность и отсутствие петли --------------------------------------
def test_marker_is_consumed_exactly_once(tmp_path: Path) -> None:
    rr.write_request(tmp_path, REPO, "2.34.0", now=NOW)

    first = rr.consume(tmp_path, REPO, now=NOW + 1)
    second = rr.consume(tmp_path, REPO, now=NOW + 2)

    assert first.accepted
    assert second.status == "none"
    assert not rr.marker_path(tmp_path).exists()


def test_replaying_the_same_marker_is_rejected(tmp_path: Path) -> None:
    """Если удаление не прошло (файл залочен) — спасает журнал nonce'ов."""
    payload = rr.write_request(tmp_path, REPO, "2.34.0", now=NOW)
    assert rr.consume(tmp_path, REPO, now=NOW + 1).accepted

    rr._atomic_write_json(rr.marker_path(tmp_path), payload)  # тот же nonce
    os.utime(rr.marker_path(tmp_path), (NOW + 100, NOW + 100))

    decision = rr.consume(tmp_path, REPO, now=NOW + 101)

    assert not decision.accepted
    assert "replay" in decision.reason


def test_second_fresh_request_within_the_cooldown_is_refused(tmp_path: Path) -> None:
    rr.write_request(tmp_path, REPO, "2.34.0", now=NOW)
    assert rr.consume(tmp_path, REPO, now=NOW).accepted

    rr.write_request(tmp_path, REPO, "2.34.0", now=NOW + 2)  # новый nonce
    decision = rr.consume(tmp_path, REPO, now=NOW + 2)

    assert not decision.accepted
    assert "cooldown" in decision.reason


def test_new_request_after_the_cooldown_is_accepted(tmp_path: Path) -> None:
    rr.write_request(tmp_path, REPO, "2.34.0", now=NOW)
    assert rr.consume(tmp_path, REPO, now=NOW).accepted

    later = NOW + rr.MIN_INTERVAL_SECONDS + 5
    rr.write_request(tmp_path, REPO, "2.34.1", now=later)

    assert rr.consume(tmp_path, REPO, now=later).accepted


def test_no_marker_is_a_quiet_no_op(tmp_path: Path) -> None:
    decision = rr.consume(tmp_path, REPO, now=NOW)

    assert decision.status == "none"
    assert decision.payload is None


# --- владелец файла --------------------------------------------------------
def test_owner_mismatch_is_rejected() -> None:
    """SID владельца маркера обязан совпасть с SID каталога данных."""
    decision = rr.validate(
        json.dumps(rr.build_payload(REPO, "2.34.0", now=NOW)),
        repo=REPO,
        now=NOW,
        owner_ok=False,
        owner_reason="owner mismatch: marker S-1-5-21-9 != data dir S-1-5-21-1",
    )

    assert not decision.accepted
    assert "owner mismatch" in decision.reason


def test_owner_check_is_skipped_when_the_sid_cannot_be_read(tmp_path: Path) -> None:
    """Не-NTFS/не-Windows: SID не определить — это не повод отвергать."""
    ok, reason = rr._owner_verdict(tmp_path / "nope", tmp_path / "nope")

    assert ok is True
    assert "unavailable" in reason


def test_owner_of_a_file_we_just_wrote_matches_its_directory(tmp_path: Path) -> None:
    if os.name != "nt":
        return
    rr.write_request(tmp_path, REPO, "2.34.0", now=NOW)

    ok, reason = rr._owner_verdict(rr.marker_path(tmp_path), tmp_path)

    assert ok is True, reason


# --- журнал результата -----------------------------------------------------
def test_result_roundtrip(tmp_path: Path) -> None:
    rr.write_result(tmp_path, nonce="abc", status="failed", detail="could not kill")

    result = rr.read_result(tmp_path)

    assert result is not None
    assert result["status"] == "failed"
    assert result["detail"] == "could not kill"


def test_unreadable_result_is_none(tmp_path: Path) -> None:
    assert rr.read_result(tmp_path) is None
    rr.result_path(tmp_path).write_text("{broken", encoding="utf-8")
    assert rr.read_result(tmp_path) is None
