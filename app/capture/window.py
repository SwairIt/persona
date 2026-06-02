"""Active window detection — Windows-focused, graceful fallback elsewhere."""

from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ActiveWindow:
    """Foreground window metadata."""

    title: str
    app_name: str
    process_name: str


def get_active_window() -> ActiveWindow | None:
    """Return the currently foreground window's metadata.

    Returns None when no foreground window can be detected (locked screen,
    permissions denied, or unsupported platform). On Windows uses ctypes
    user32 + psutil. On other platforms returns None for v0.
    """
    if sys.platform == "win32":
        return _get_active_window_windows()
    return None


def _get_active_window_windows() -> ActiveWindow | None:
    try:
        import ctypes
        from ctypes import wintypes

        import psutil
    except ImportError:
        return None

    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None

    length = user32.GetWindowTextLengthW(hwnd)
    title_buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, title_buffer, length + 1)
    title = title_buffer.value or ""

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    process_name = ""
    app_name = ""
    if pid.value:
        try:
            proc = psutil.Process(pid.value)
            process_name = proc.name()
            app_name = _derive_app_name(process_name, title)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            process_name = ""

    if not title and not process_name:
        return None

    return ActiveWindow(
        title=title.strip(),
        app_name=app_name,
        process_name=process_name,
    )


def _derive_app_name(process_name: str, title: str) -> str:
    """Best-effort human-readable app name from process and window title."""
    base = process_name.lower().removesuffix(".exe")
    known = {
        "chrome": "Google Chrome",
        "msedge": "Microsoft Edge",
        "firefox": "Firefox",
        "code": "VS Code",
        "code - insiders": "VS Code Insiders",
        "windowsterminal": "Windows Terminal",
        "powershell": "PowerShell",
        "pwsh": "PowerShell",
        "explorer": "Windows Explorer",
        "telegram": "Telegram",
        "slack": "Slack",
        "discord": "Discord",
        "spotify": "Spotify",
        "notepad": "Notepad",
        "notepad++": "Notepad++",
        "obsidian": "Obsidian",
        "figma": "Figma",
        "blender": "Blender",
        "steam": "Steam",
        "pycharm64": "PyCharm",
        "idea64": "IntelliJ IDEA",
        "rider64": "JetBrains Rider",
        "webstorm64": "WebStorm",
    }
    if base in known:
        return known[base]
    if title and " - " in title:
        tail = title.rsplit(" - ", maxsplit=1)[-1].strip()
        if 2 < len(tail) < 40:
            return tail
    return base.title() if base else "Unknown"
