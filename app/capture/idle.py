"""Idle-time detection — Windows GetLastInputInfo, fallback returns 0."""

from __future__ import annotations

import sys


def seconds_since_last_input() -> float:
    """Return seconds since the last keyboard/mouse input.

    On Windows uses GetLastInputInfo via ctypes. On other platforms
    returns 0.0 (capture proceeds as if user is always active).
    """
    if sys.platform == "win32":
        return _seconds_since_last_input_windows()
    return 0.0


def _seconds_since_last_input_windows() -> float:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:
        return 0.0

    class LASTINPUTINFO(ctypes.Structure):
        _fields_ = (
            ("cbSize", wintypes.UINT),
            ("dwTime", wintypes.DWORD),
        )

    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0.0

    ticks_now = ctypes.windll.kernel32.GetTickCount()
    elapsed_ms = max(0, ticks_now - lii.dwTime)
    return elapsed_ms / 1000.0


def is_screen_locked() -> bool:
    """Best-effort check whether the workstation is locked. Windows only."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
    except ImportError:
        return False
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if hwnd == 0:
        return True
    return False
