"""Tests for process whitelist."""

from __future__ import annotations

from app.capture.whitelist import (
    default_deny_list,
    load_user_lists,
    save_user_lists,
    should_capture,
)


def test_should_capture_allows_normal_process() -> None:
    assert should_capture("code.exe") is True


def test_should_capture_blocks_built_in_password_manager() -> None:
    assert should_capture("1password.exe") is False
    assert should_capture("KeePass.exe") is False
    assert should_capture("BITWARDEN.EXE") is False


def test_should_capture_blocks_built_in_games() -> None:
    assert should_capture("dota2.exe") is False
    assert should_capture("CSGO.EXE") is False


def test_should_capture_accepts_none_process() -> None:
    assert should_capture(None) is True
    assert should_capture("") is True


def test_user_deny_list_persists() -> None:
    save_user_lists(deny=["zoom.exe"], allow_only=[])
    assert should_capture("zoom.exe") is False
    assert should_capture("code.exe") is True
    loaded = load_user_lists()
    assert "zoom.exe" in loaded["deny"]


def test_allow_only_mode_strict() -> None:
    save_user_lists(deny=[], allow_only=["code.exe", "pycharm64.exe"])
    assert should_capture("code.exe") is True
    assert should_capture("chrome.exe") is False
    assert should_capture("pycharm64.exe") is True


def test_default_deny_list_is_nonempty() -> None:
    defaults = default_deny_list()
    assert len(defaults) > 5
    assert all(isinstance(p, str) for p in defaults)
