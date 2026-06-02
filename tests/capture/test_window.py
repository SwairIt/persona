"""Tests for window-detection helpers — platform-independent surface."""

from __future__ import annotations

from app.capture.window import _derive_app_name, get_active_window


def test_derive_app_name_known_processes() -> None:
    assert _derive_app_name("chrome.exe", "Some site - Google Chrome") == "Google Chrome"
    assert _derive_app_name("code.exe", "main.py - VS Code") == "VS Code"
    assert _derive_app_name("pwsh.exe", "Windows Terminal") == "PowerShell"


def test_derive_app_name_falls_back_to_title_tail() -> None:
    result = _derive_app_name("unknown.exe", "Project Persona - Some App")
    assert result == "Some App"


def test_derive_app_name_falls_back_to_titled_process() -> None:
    assert _derive_app_name("custom.exe", "") == "Custom"
    assert _derive_app_name("", "") == "Unknown"


def test_get_active_window_returns_optional() -> None:
    result = get_active_window()
    assert result is None or hasattr(result, "title")
