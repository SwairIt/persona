"""Windows clipboard text reader for opt-in history capture.

Exposes :func:`read_clipboard_text`, an async helper that returns the
current clipboard contents as a ``str`` (or ``None`` if the clipboard
is empty, holds non-text data, or the Win32 calls fail).

Why ctypes and not ``pyperclip`` / ``pywin32``?
    * Zero new dependencies — this module is imported by the always-on
      worker loop and we don't want to drag a fresh package in just for
      three WinAPI calls.
    * Explicit ``CF_UNICODETEXT`` handling — pyperclip historically used
      ``CF_TEXT`` (legacy codepage) on Windows and would mangle non-ASCII.
    * We can fail-open quietly — any clipboard error returns ``None`` and
      the worker keeps polling. A noisy raise would spam the log.

Cross-platform safety: on non-Windows platforms the function returns
``None`` immediately. Tests run on every OS so the worker import must
not blow up on Linux CI.

Also exposes :func:`hash_text` — SHA-256 hex digest used by the worker
to dedupe back-to-back identical reads.
"""

from __future__ import annotations

import hashlib
import sys

import anyio

from app.logging_setup import get_logger

log = get_logger("persona.clipboard")

_CF_UNICODETEXT = 13
# Retry budget for OpenClipboard — another process (browser, IME) may
# hold it briefly. We back off in the worker, but a couple of in-call
# retries dodge the common one-millisecond race.
_OPEN_RETRIES = 3


def hash_text(text: str) -> str:
    """Return SHA-256 hex digest of ``text`` (UTF-8 encoded)."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


async def read_clipboard_text() -> str | None:
    """Return the current clipboard text, or ``None`` if unavailable.

    Async wrapper that delegates to a synchronous WinAPI call on a
    worker thread so the polling loop never blocks the event loop.
    """
    if sys.platform != "win32":
        return None
    return await anyio.to_thread.run_sync(_read_clipboard_text_windows)


def _bind_winapi() -> tuple[object, object] | None:
    """Resolve user32 + kernel32 with the argtypes we need.

    Returns ``(user32, kernel32)`` on success, or ``None`` if anything
    (import, DLL load, attribute bind) goes wrong — the caller treats
    failure as "clipboard unavailable" and gives up quietly.
    """
    try:
        import ctypes  # noqa: PLC0415 — windows-only conditional import
        from ctypes import wintypes  # noqa: PLC0415 — windows-only conditional import
    except ImportError:
        return None

    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = ctypes.c_void_p
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
    except (AttributeError, OSError):
        return None
    return user32, kernel32


def _extract_locked_text(user32: object, kernel32: object) -> str | None:
    """Read CF_UNICODETEXT from an already-opened clipboard.

    Caller owns OpenClipboard / CloseClipboard. Returns the snippet, or
    ``None`` if the handle is empty / lock fails / WinAPI raises.
    """
    try:
        import ctypes  # noqa: PLC0415 — needed for wstring_at on locked buffer
    except ImportError:
        return None
    try:
        handle = user32.GetClipboardData(_CF_UNICODETEXT)  # type: ignore[attr-defined]
        if not handle:
            return None
        locked = kernel32.GlobalLock(handle)  # type: ignore[attr-defined]
        if not locked:
            return None
        try:
            text = ctypes.wstring_at(locked)
        finally:
            kernel32.GlobalUnlock(handle)  # type: ignore[attr-defined]
    except OSError:
        return None
    return text or None


def _read_clipboard_text_windows() -> str | None:
    bindings = _bind_winapi()
    if bindings is None:
        return None
    user32, kernel32 = bindings

    try:
        if not user32.IsClipboardFormatAvailable(_CF_UNICODETEXT):  # type: ignore[attr-defined]
            return None
    except OSError:
        return None

    opened = False
    for _ in range(_OPEN_RETRIES):
        try:
            if user32.OpenClipboard(None):  # type: ignore[attr-defined]
                opened = True
                break
        except OSError:
            return None
    if not opened:
        return None

    try:
        return _extract_locked_text(user32, kernel32)
    finally:
        try:
            user32.CloseClipboard()  # type: ignore[attr-defined]
        except OSError:
            log.debug("clipboard.close_failed")
