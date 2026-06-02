"""Windows session-lock detection.

Exposes :func:`is_session_locked`, an async helper that returns ``True``
when the active console session is currently locked (Win+L, secure
desktop, etc). The capture loop uses this to skip iterations that would
only produce useless lock-screen frames.

Implementation:
    * Windows: ``WTSGetActiveConsoleSessionId`` + ``WTSQuerySessionInformationW``
      with ``WTSSessionInfoEx`` → check ``SessionFlags`` against
      ``WTS_SESSIONSTATE_LOCK``.
    * Other platforms: always ``False`` (capture continues).
    * Any ctypes/WinAPI failure: log ``session.detect_failed`` and return
      ``False`` — we fail open so a broken detector cannot freeze capture.
"""

from __future__ import annotations

import sys

import anyio

from app.logging_setup import get_logger

log = get_logger("persona.session_state")

# Per MSDN: WTSINFOEX_LEVEL1 SessionFlags values.
# WTS_SESSIONSTATE_LOCK == 0, WTS_SESSIONSTATE_UNLOCK == 1.
# On Windows Server 2008 / Windows 7 the values are inverted due to a
# documented bug; we treat both extremes as "locked == 0" since modern
# Windows 10/11 ships the corrected semantics.
_WTS_SESSIONSTATE_LOCK = 0
_WTS_CURRENT_SERVER_HANDLE = 0
_WTS_INFO_CLASS_SESSION_INFO_EX = 25


async def is_session_locked() -> bool:
    """Return ``True`` if the Windows workstation is currently locked.

    Non-Windows platforms always return ``False``. Any error from the
    underlying Win32 calls is swallowed (logged at warning level) and
    treated as "not locked" so capture keeps running.
    """
    if sys.platform != "win32":
        return False
    return await anyio.to_thread.run_sync(_is_session_locked_windows)


def _is_session_locked_windows() -> bool:  # noqa: PLR0911 — fail-open guard rails on every WinAPI step
    try:
        import ctypes  # noqa: PLC0415 — windows-only conditional import
        from ctypes import wintypes  # noqa: PLC0415 — windows-only conditional import
    except ImportError:
        log.warning("session.detect_failed", reason="ctypes_unavailable")
        return False

    try:
        wtsapi32 = ctypes.windll.wtsapi32
        kernel32 = ctypes.windll.kernel32
    except (OSError, AttributeError) as exc:
        log.warning("session.detect_failed", reason="dll_unavailable", error=str(exc))
        return False

    class WTSINFOEX_LEVEL1_W(ctypes.Structure):
        _fields_ = (
            ("SessionId", wintypes.DWORD),
            ("SessionState", ctypes.c_int),
            ("SessionFlags", wintypes.LONG),
            ("WinStationName", wintypes.WCHAR * 33),
            ("UserName", wintypes.WCHAR * 21),
            ("DomainName", wintypes.WCHAR * 18),
            ("LogonTime", wintypes.LARGE_INTEGER),
            ("ConnectTime", wintypes.LARGE_INTEGER),
            ("DisconnectTime", wintypes.LARGE_INTEGER),
            ("LastInputTime", wintypes.LARGE_INTEGER),
            ("CurrentTime", wintypes.LARGE_INTEGER),
            ("IncomingBytes", wintypes.DWORD),
            ("OutgoingBytes", wintypes.DWORD),
            ("IncomingFrames", wintypes.DWORD),
            ("OutgoingFrames", wintypes.DWORD),
            ("IncomingCompressedBytes", wintypes.DWORD),
            ("OutgoingCompressedBytes", wintypes.DWORD),
        )

    class WTSINFOEX_LEVEL_W(ctypes.Union):
        _fields_ = (("WTSInfoExLevel1", WTSINFOEX_LEVEL1_W),)

    class WTSINFOEXW(ctypes.Structure):
        _fields_ = (
            ("Level", wintypes.DWORD),
            ("Data", WTSINFOEX_LEVEL_W),
        )

    try:
        wtsapi32.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
        wtsapi32.WTSGetActiveConsoleSessionId.argtypes = []
        wtsapi32.WTSQuerySessionInformationW.restype = wintypes.BOOL
        wtsapi32.WTSQuerySessionInformationW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.POINTER(ctypes.POINTER(WTSINFOEXW)),
            ctypes.POINTER(wintypes.DWORD),
        ]
        wtsapi32.WTSFreeMemory.restype = None
        wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    except (AttributeError, OSError) as exc:
        log.warning("session.detect_failed", reason="bind_failed", error=str(exc))
        return False

    try:
        session_id = wtsapi32.WTSGetActiveConsoleSessionId()
        if session_id == 0xFFFFFFFF:
            # No attached console session — treat as locked-ish, but
            # fail-open semantics say return False.
            log.warning("session.detect_failed", reason="no_console_session")
            return False

        buffer_ptr = ctypes.POINTER(WTSINFOEXW)()
        bytes_returned = wintypes.DWORD(0)
        ok = wtsapi32.WTSQuerySessionInformationW(
            _WTS_CURRENT_SERVER_HANDLE,
            session_id,
            _WTS_INFO_CLASS_SESSION_INFO_EX,
            ctypes.byref(buffer_ptr),
            ctypes.byref(bytes_returned),
        )
        if not ok or not buffer_ptr:
            err = kernel32.GetLastError()
            log.warning("session.detect_failed", reason="wts_query_failed", code=int(err))
            return False

        try:
            info = buffer_ptr.contents
            if info.Level != 1:
                return False
            session_flags = int(info.Data.WTSInfoExLevel1.SessionFlags)
        finally:
            wtsapi32.WTSFreeMemory(buffer_ptr)
    except OSError as exc:
        log.warning("session.detect_failed", reason="winapi_error", error=str(exc))
        return False

    return session_flags == _WTS_SESSIONSTATE_LOCK
