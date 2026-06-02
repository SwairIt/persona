"""Extract and cache app icons from Windows process executables.

Best-effort: many processes (anti-cheat, system) won't yield an icon.
We swallow failures silently and just don't cache anything for that app.
Icons are cached as 32×32 PNG inside `data/icons/`. Lookup is by
process-name (lowercased, .exe stripped).
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image

from app.logging_setup import get_logger
from app.settings import get_settings

log = get_logger("persona.icons")

ICON_SIZE = 32


def icon_path_for(process_name: str | None) -> Path | None:
    if not process_name:
        return None
    key = _key(process_name)
    if not key:
        return None
    return _icons_dir() / f"{key}.png"


def ensure_icon_cached(process_name: str | None) -> Path | None:
    """If we don't have a cached icon for this app, try to extract one. Best-effort."""
    if sys.platform != "win32":
        return icon_path_for(process_name) if process_name else None
    path = icon_path_for(process_name)
    if path is None:
        return None
    if path.exists():
        return path

    image = _extract_icon_windows(process_name or "")
    if image is None:
        return None

    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        image.save(path, format="PNG", optimize=True)
    except OSError as exc:
        log.warning("icons.save_failed", path=str(path), error=str(exc))
        return None
    return path


def _icons_dir() -> Path:
    return get_settings().data_dir / "icons"


def _key(process_name: str) -> str:
    base = process_name.strip().lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return "".join(c for c in base if c.isalnum() or c in "-_")


def _extract_icon_windows(process_name: str) -> Image.Image | None:
    """Walk running processes; if one matches the name, pull its icon. Return None if not found."""
    try:
        import ctypes
        from ctypes import wintypes

        import psutil
    except ImportError:
        return None

    target = process_name.lower()
    candidate_path: Path | None = None
    for proc in psutil.process_iter(["name", "exe"]):
        try:
            name = (proc.info.get("name") or "").lower()
            if name == target and proc.info.get("exe"):
                candidate_path = Path(proc.info["exe"])
                break
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if candidate_path is None or not candidate_path.exists():
        return None

    shell32 = ctypes.windll.shell32
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    large = (ctypes.c_void_p * 1)()
    small = (ctypes.c_void_p * 1)()
    extracted = shell32.ExtractIconExW(
        ctypes.c_wchar_p(str(candidate_path)), 0, large, small, 1
    )
    if not extracted:
        return None

    try:
        return _hicon_to_pil(large[0] or small[0], user32, gdi32)
    finally:
        for handle in (large[0], small[0]):
            if handle:
                user32.DestroyIcon(handle)


def _hicon_to_pil(hicon: int, user32, gdi32) -> Image.Image | None:  # type: ignore[no-untyped-def]
    import ctypes
    from ctypes import wintypes

    class ICONINFO(ctypes.Structure):
        _fields_ = (
            ("fIcon", wintypes.BOOL),
            ("xHotspot", wintypes.DWORD),
            ("yHotspot", wintypes.DWORD),
            ("hbmMask", wintypes.HBITMAP),
            ("hbmColor", wintypes.HBITMAP),
        )

    class BITMAP(ctypes.Structure):
        _fields_ = (
            ("bmType", wintypes.LONG),
            ("bmWidth", wintypes.LONG),
            ("bmHeight", wintypes.LONG),
            ("bmWidthBytes", wintypes.LONG),
            ("bmPlanes", wintypes.WORD),
            ("bmBitsPixel", wintypes.WORD),
            ("bmBits", ctypes.c_void_p),
        )

    info = ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(info)):
        return None

    bmp = BITMAP()
    gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(BITMAP), ctypes.byref(bmp))
    width = bmp.bmWidth
    height = bmp.bmHeight
    if width <= 0 or height <= 0:
        return None

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = (
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        )

    bi = BITMAPINFOHEADER()
    bi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bi.biWidth = width
    bi.biHeight = -height
    bi.biPlanes = 1
    bi.biBitCount = 32
    bi.biCompression = 0

    buffer_size = width * height * 4
    buffer = (ctypes.c_byte * buffer_size)()

    hdc = user32.GetDC(0)
    try:
        gdi32.GetDIBits(hdc, info.hbmColor, 0, height, buffer, ctypes.byref(bi), 0)
    finally:
        user32.ReleaseDC(0, hdc)
        gdi32.DeleteObject(info.hbmColor)
        gdi32.DeleteObject(info.hbmMask)

    try:
        image = Image.frombuffer("RGBA", (width, height), bytes(buffer), "raw", "BGRA", 0, 1)
    except (ValueError, OSError):
        return None

    if image.width != ICON_SIZE:
        image = image.resize((ICON_SIZE, ICON_SIZE), Image.Resampling.LANCZOS)
    return image
