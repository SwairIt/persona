"""Tests for idle detection — surface-level (no real user input)."""

from __future__ import annotations

import sys

from app.capture.idle import is_screen_locked, seconds_since_last_input


def test_seconds_since_last_input_returns_nonneg() -> None:
    value = seconds_since_last_input()
    assert value >= 0


def test_is_screen_locked_returns_bool() -> None:
    result = is_screen_locked()
    assert isinstance(result, bool)


def test_idle_unsupported_platform_returns_zero(monkeypatch) -> None:
    if sys.platform == "win32":
        return
    assert seconds_since_last_input() == 0.0
