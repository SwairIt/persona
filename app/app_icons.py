"""Per-app icon cache — DB-backed, deterministic, cross-platform-friendly.

Every screenshot row carries an ``app_name`` (Win32 window-class or
process-executable name — ``Slack``, ``chrome.exe``, ``devenv.exe``).
The UI wants a small icon next to that string in the timeline / tags
chips. This module is the cache layer that backs ``/app-icon/{name}.png``:

1. Lookup by ``app_name`` in the ``app_icon`` SQLite table
   (see :mod:`app.storage.migrations.044_app_icons`).
2. On Windows, walk ``psutil.process_iter`` to find a running process
   whose ``name`` matches ``app_name``, then ask the shell for its
   icon via ``SHGetFileInfoW`` with the ``SHGFI_ICON`` flag. The
   resulting ``HICON`` is rendered to a 64x64 PNG and cached with
   ``source='windows_exe'``. This is v1.9's headline upgrade over the
   v0.45 ``ExtractIconExW`` path — ``SHGetFileInfoW`` honours
   per-version icon overlays the shell associates with the exe
   (Steam, Electron wrappers, MSIX-installed apps) instead of always
   returning the first icon resource baked into the binary.
3. On Windows with the legacy ``PERSONA_APP_ICONS_USE_SHELL32`` opt-in,
   fall back to the older :mod:`app.capture.icons` ``ExtractIconExW``
   path. Kept as a safety net in case ``SHGetFileInfoW`` mis-renders on
   some exotic exe; opt-out is a single env var.
4. Otherwise (the default cross-platform path), generate a
   deterministic "initials" PNG: 64x64 RGB tile, background hue
   derived from a stable SHA-256 of the lowercased ``app_name``,
   centred uppercase first-two letters in white. Stored with
   ``source='initials'``.

The generator is intentionally deterministic so the *same* app gets the
*same* tile across reinstalls, browser refreshes and across machines.

All PIL and ctypes work runs inside :func:`anyio.to_thread.run_sync` so
the calling coroutine never blocks the event loop on disk IO, pixel
arithmetic or Win32 shell round-trips.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import time
from pathlib import Path
from typing import Final

import anyio
from PIL import Image, ImageDraw, ImageFont

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.app_icons")
# Dedicated child logger for the SHGetFileInfoW path so an operator can
# isolate the v1.9 extraction trail from the generic icon-cache logs
# (the legacy ExtractIconExW route still logs under ``persona.app_icons``).
win_log = get_logger("persona.app_icons.windows")

# Final on-disk tile size. 64 is the largest "small icon" most desktop
# environments still ship at native resolution; bigger PNGs would just
# be a Lanczos upscale of a 32x32 source.
_TILE_SIZE: Final[int] = 64

# Initials text colour. White on a saturated hue reads at any size and
# avoids the per-app contrast tuning a darker palette would need.
_TEXT_RGB: Final[tuple[int, int, int]] = (255, 255, 255)

# HSL→RGB ladder constants for the initials fallback. ``_SAT`` and
# ``_LUM`` are tuned so two random hues land far enough apart to be
# distinguishable but never wash out the white letters.
_SAT: Final[float] = 0.55
_LUM: Final[float] = 0.42

# Valid ``source`` column values — kept in sync with the migration's
# docstring so a future "select rows by source" maintenance query has a
# single source of truth.
#
# ``user`` was added in v0.58 to let an operator override the
# auto-generated tile with a hand-picked PNG (see
# :mod:`app.web.routes.app_icons` upload endpoint). The lookup path in
# :func:`get_icon_png` treats it like any other cached row — the only
# place ``source`` matters is the admin UI which renders a "custom"
# badge so the operator can tell which rows survive a regenerate.
_SOURCE_SHELL32: Final[str] = "shell32"
_SOURCE_INITIALS: Final[str] = "initials"
_SOURCE_USER: Final[str] = "user"
# v1.9 — ``SHGetFileInfoW`` + ``SHGFI_ICON`` path. Kept distinct from the
# legacy ``shell32`` source so a future maintenance job can selectively
# re-extract one variant without nuking the other, and so the admin UI
# can flag "this row came from the new pipeline".
_SOURCE_WINDOWS_EXE: Final[str] = "windows_exe"

# Observability budget for the Win32 Shell32 extraction path.
# ``psutil.process_iter`` can walk thousands of processes on a busy
# machine; if a single extraction takes longer than this many seconds
# we log it so an operator can investigate. We do not cancel mid-flight
# (ctypes calls cannot be safely interrupted) — the cache means each
# slow scan happens at most once per app.
_SHELL32_BUDGET_SECONDS: Final[float] = 2.0

# Env-var name that opts a deployment in to the Shell32 extraction path.
# Anything other than ``"1"``, ``"true"``, ``"yes"`` (case-insensitive)
# leaves the initials-only behaviour in place.
_SHELL32_OPT_IN_ENV: Final[str] = "PERSONA_APP_ICONS_USE_SHELL32"
_SHELL32_OPT_IN_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def get_icon_png(app_name: str) -> bytes:
    """Return PNG bytes for ``app_name`` — cached, or generated and cached.

    Lookup order:

    1. ``app_icon`` row keyed by the normalised ``app_name``. The row may
       have ``source='user'`` (an operator-uploaded override — v0.58),
       ``source='windows_exe'`` (v1.9 ``SHGetFileInfoW`` extraction),
       ``source='shell32'`` (legacy ``ExtractIconExW`` extraction) or
       ``source='initials'`` (deterministic fallback). All four are
       returned as-is — *the user override takes precedence simply by
       virtue of being the row that's there*, because the upload path
       writes it with ``ON CONFLICT DO UPDATE`` and the reset path
       deletes the row so the next read falls through to (2).
    2. On miss, generate (Windows exe attempt → optional legacy Shell32
       attempt → initials fallback) and persist with the appropriate
       non-user ``source``.

    Never raises for empty / unusable input: an empty name yields the
    same deterministic "??" tile as any other unrecognised string, so
    the route never has to special-case it.
    """
    key = _normalise_key(app_name)

    cached = await _load_cached(key)
    if cached is not None:
        return cached

    png_bytes, source = await _generate(key)
    await _store(key, png_bytes, source)
    log.info("app_icons.generated", app_name=key, source=source, bytes=len(png_bytes))
    return png_bytes


async def store_user_icon(app_name: str, png_bytes: bytes) -> None:
    """Persist ``png_bytes`` as the operator-chosen icon for ``app_name``.

    Writes with ``source='user'`` so :func:`get_icon_png` returns it on
    every subsequent call until :func:`invalidate` (or the admin reset
    endpoint) drops the row. Validation of the bytes (PNG magic, decoded
    dimensions, byte ceiling) is the *caller's* responsibility — this
    helper is the storage primitive, not the policy boundary.
    """
    key = _normalise_key(app_name)
    await _store(key, png_bytes, _SOURCE_USER)
    log.info(
        "app_icons.user_stored",
        app_name=key,
        bytes=len(png_bytes),
    )


async def invalidate(app_name: str) -> None:
    """Drop the cached row for ``app_name`` so the next call regenerates.

    Idempotent — deleting a missing row is not an error. Used by both
    the reset endpoint (admin-triggered "go back to the auto-generated
    tile") and any future maintenance job that wants to force a
    re-extract.
    """
    key = _normalise_key(app_name)
    async with get_connection() as conn:
        await conn.execute("DELETE FROM app_icon WHERE app_name = ?", (key,))
        await conn.commit()
    log.info("app_icons.invalidated", app_name=key)


async def list_known_icons() -> list[dict[str, str]]:
    """Return one row per cached app icon, oldest-first by app_name.

    Used by the v0.58 admin page to render the table of known apps with
    their current icon source. Each item exposes ``app_name`` and
    ``source`` (one of ``windows_exe`` / ``shell32`` / ``initials`` /
    ``user``) so the template can flag custom overrides and real-exe
    icons without a second query.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT app_name, source FROM app_icon ORDER BY app_name ASC",
        )
        rows = await cursor.fetchall()
    return [{"app_name": str(row["app_name"]), "source": str(row["source"])} for row in rows]


async def get_icon_source(app_name: str) -> str | None:
    """Return the cached ``source`` for ``app_name`` or ``None`` if absent.

    Cheap metadata-only lookup — does not load the PNG blob. The admin
    page uses it to render the "auto / custom" badge next to apps that
    have never had their icon viewed (no row yet) without paying the
    cost of fetching the BLOB column.
    """
    key = _normalise_key(app_name)
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT source FROM app_icon WHERE app_name = ?",
            (key,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return str(row["source"])


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------


async def _load_cached(app_name: str) -> bytes | None:
    """Fetch the cached PNG bytes for ``app_name``, or ``None`` if absent."""
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT png_bytes FROM app_icon WHERE app_name = ?",
            (app_name,),
        )
        row = await cursor.fetchone()
    if row is None:
        return None
    return bytes(row["png_bytes"])


async def _store(app_name: str, png_bytes: bytes, source: str) -> None:
    """Insert-or-replace the cached row for ``app_name``."""
    async with get_connection() as conn:
        await conn.execute(
            """
            INSERT INTO app_icon (app_name, png_bytes, source)
            VALUES (?, ?, ?)
            ON CONFLICT(app_name) DO UPDATE SET
                png_bytes = excluded.png_bytes,
                source = excluded.source,
                generated_at = datetime('now')
            """,
            (app_name, png_bytes, source),
        )
        await conn.commit()


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


async def _generate(app_name: str) -> tuple[bytes, str]:
    """Produce a fresh PNG for ``app_name``. Returns ``(bytes, source)``.

    Both the Shell32 attempt and the initials fallback run inside a
    single :func:`anyio.to_thread.run_sync` call so we never abandon a
    half-finished thread under timeout — abandoned threads stuck inside
    ``psutil.process_iter`` would hold the GIL and starve subsequent
    icon requests on the same pool.
    """
    return await anyio.to_thread.run_sync(_generate_sync, app_name)


def _generate_sync(app_name: str) -> tuple[bytes, str]:
    """Worker for :func:`_generate`. Runs entirely off the event loop.

    Generation cascade on Windows:

    1. ``SHGetFileInfoW`` against the running process's exe path — the
       v1.9 path. Best quality (honours shell overlays), cheap when the
       exe is already running.
    2. Legacy :mod:`app.capture.icons` ``ExtractIconExW`` path, gated by
       ``PERSONA_APP_ICONS_USE_SHELL32``. Only attempted when (1) misses
       *and* an operator opted in — kept as a safety net while the new
       pipeline matures.
    3. Deterministic initials tile — the unconditional cross-platform
       fallback.

    Any miss or failure transparently falls through to the next step so
    a slow / unavailable extractor never propagates a user-visible error.
    """
    if sys.platform == "win32":
        # The cascade promises "any miss or failure falls through", and the
        # route above promises it never fails ("every string yields a valid
        # PNG"). Step 1 walks live processes and touches paths owned by other
        # Windows accounts, so it can raise things no caller can enumerate in
        # advance — the concrete case that hit production was a
        # PermissionError out of ``Path.exists()``. Make the promise true
        # here rather than trusting every future Win32 edge to behave.
        try:
            windows_png = _extract_windows_exe_png(app_name)
        except Exception as exc:  # noqa: BLE001 — the initials tile is the floor
            log.warning(
                "app_icons.windows_exe_failed",
                app_name=app_name,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            windows_png = None
        if windows_png is not None:
            return windows_png, _SOURCE_WINDOWS_EXE

        if _shell32_opted_in():
            start = time.monotonic()
            shell_png = _extract_shell32_png(app_name)
            elapsed = time.monotonic() - start
            if shell_png is not None:
                log.info(
                    "app_icons.shell32_extracted",
                    app_name=app_name,
                    elapsed_seconds=round(elapsed, 3),
                )
                return shell_png, _SOURCE_SHELL32
            if elapsed >= _SHELL32_BUDGET_SECONDS:
                log.info(
                    "app_icons.shell32_slow_miss",
                    app_name=app_name,
                    elapsed_seconds=round(elapsed, 3),
                )
    return _render_initials_png(app_name), _SOURCE_INITIALS


def _shell32_opted_in() -> bool:
    """Return True if the deployment has opted in to Shell32 extraction."""
    raw = os.environ.get(_SHELL32_OPT_IN_ENV, "").strip().lower()
    return raw in _SHELL32_OPT_IN_TRUTHY


def _extract_shell32_png(app_name: str) -> bytes | None:
    """Try to extract the real exe icon on Windows. Returns PNG bytes or None.

    Reuses :mod:`app.capture.icons` so we don't duplicate the ctypes /
    ``ExtractIconExW`` plumbing. The returned PIL image is whatever size
    the source icon had (32x32 in practice); we re-sample to 64x64 so
    the cache layer is shape-uniform.
    """
    try:
        from app.capture.icons import _extract_icon_windows  # noqa: PLC0415 — Windows-only
    except ImportError:
        return None
    try:
        image = _extract_icon_windows(app_name)
    except Exception as exc:
        # Best-effort: a missing process or anti-cheat-protected exe must
        # never bubble; the caller falls back to the initials tile.
        log.debug("app_icons.shell32_failed", app_name=app_name, error=str(exc))
        return None
    if image is None:
        return None
    if image.size != (_TILE_SIZE, _TILE_SIZE):
        image = image.resize((_TILE_SIZE, _TILE_SIZE), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _resolve_running_exe(app_name: str) -> Path | None:
    """Walk ``psutil.process_iter`` looking for a running exe matching ``app_name``.

    Match logic mirrors :mod:`app.capture.icons` so the two extractors
    behave identically on which apps they "see": case-insensitive
    equality between ``proc.info['name']`` and the requested ``app_name``.
    We also accept a match against the exe stem (``chrome.exe`` → match
    when the caller asked for ``chrome``) so callers can pass either the
    Win32 window-class name *or* the raw process name without the cache
    fragmenting.

    Returns the first matching path that still exists on disk, or
    ``None`` if no candidate is found / psutil is unavailable. Suppresses
    ``NoSuchProcess`` / ``AccessDenied`` *and* ``OSError`` per-row because
    neither a vanishing process nor an unreadable path must abort the scan.

    The ``OSError`` arm is load-bearing on Windows, not defensive padding.
    ``Path.exists()`` re-raises anything other than "not found" — a
    ``PermissionError`` (WinError 5) is *not* swallowed — and a Persona
    server running as one Windows account routinely sees processes whose
    exe lives under a different user's protected profile
    (``C:\\Users\\<other>\\AppData\\Roaming\\...``). Every
    ``GET /app-icon/{app}.png`` for such an app was a hard 500.
    """
    try:
        import psutil  # noqa: PLC0415 — keep psutil out of import path on non-Windows
    except ImportError:
        return None

    target = app_name.strip().lower()
    target_stem = target[:-4] if target.endswith(".exe") else target

    for proc in psutil.process_iter(["name", "exe"]):
        try:
            raw_name = (proc.info.get("name") or "").lower()
            if not raw_name:
                continue
            raw_stem = raw_name[:-4] if raw_name.endswith(".exe") else raw_name
            if raw_name == target or raw_stem == target_stem:
                exe = proc.info.get("exe")
                if exe:
                    candidate = Path(exe)
                    if candidate.exists():
                        return candidate
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return None


def _extract_windows_exe_png(app_name: str) -> bytes | None:
    """Extract the shell-rendered exe icon via ``SHGetFileInfoW``.

    Windows-only. Returns PNG bytes on success or ``None`` for any miss:
    no running process matches ``app_name``, ``SHGetFileInfoW`` returns
    a null icon handle, ``GetIconInfo`` / ``GetDIBits`` fails, or any
    other Win32 quirk surfaces. Callers fall back to the legacy /
    initials path.

    The PIL conversion mirrors :mod:`app.capture.icons` — 32x32 BGRA
    buffer from ``GetDIBits``, Lanczos-resampled to the cache's
    canonical 64x64 — so the on-disk PNGs are shape-uniform regardless
    of which extractor produced them.

    The function is purely synchronous; the caller (:func:`_generate`)
    wraps the whole generation cascade in :func:`anyio.to_thread.run_sync`
    so neither psutil's process walk nor the ctypes round-trip ever
    blocks the event loop.
    """
    try:
        import ctypes  # noqa: PLC0415 — Windows-only stdlib
        from ctypes import wintypes  # noqa: PLC0415
    except ImportError:
        return None

    exe_path = _resolve_running_exe(app_name)
    if exe_path is None:
        win_log.debug("app_icons.windows.no_running_exe", app_name=app_name)
        return None

    start = time.monotonic()

    # SHFILEINFOW layout — fixed by the SDK headers. We only consume
    # ``hIcon``; the rest is reserved for API contract compatibility.
    class SHFILEINFOW(ctypes.Structure):
        _fields_ = (
            ("hIcon", wintypes.HICON),
            ("iIcon", ctypes.c_int),
            ("dwAttributes", wintypes.DWORD),
            ("szDisplayName", wintypes.WCHAR * 260),
            ("szTypeName", wintypes.WCHAR * 80),
        )

    SHGFI_ICON: Final[int] = 0x000000100
    SHGFI_LARGEICON: Final[int] = 0x000000000  # 32x32 system metric

    try:
        shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        gdi32 = ctypes.windll.gdi32  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        # ``ctypes.windll`` is only present on Windows; defensive in case
        # this branch is reached on a misconfigured cross-build runner.
        return None

    info = SHFILEINFOW()
    flags = SHGFI_ICON | SHGFI_LARGEICON
    result = shell32.SHGetFileInfoW(
        ctypes.c_wchar_p(str(exe_path)),
        0,
        ctypes.byref(info),
        ctypes.sizeof(info),
        flags,
    )
    if not result or not info.hIcon:
        win_log.debug(
            "app_icons.windows.shgetfileinfo_miss",
            app_name=app_name,
            exe=str(exe_path),
        )
        return None

    try:
        image = _hicon_to_pil_image(info.hIcon, user32, gdi32)
    except Exception as exc:
        # Win32 surfaces a wide menagerie of exceptions through ctypes
        # — OSError, ValueError, occasionally bare ``Exception`` from
        # PIL when the BGRA buffer is malformed. We deliberately swallow
        # all of them: a missed extraction must fall back to initials,
        # never bubble a 500 into the timeline render.
        win_log.debug(
            "app_icons.windows.hicon_decode_failed",
            app_name=app_name,
            error=str(exc),
        )
        image = None
    finally:
        user32.DestroyIcon(info.hIcon)

    if image is None:
        return None

    if image.size != (_TILE_SIZE, _TILE_SIZE):
        image = image.resize((_TILE_SIZE, _TILE_SIZE), Image.Resampling.LANCZOS)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    png_bytes = buffer.getvalue()

    elapsed = time.monotonic() - start
    win_log.info(
        "app_icons.windows.extracted",
        app_name=app_name,
        exe=str(exe_path),
        bytes=len(png_bytes),
        elapsed_seconds=round(elapsed, 3),
    )
    return png_bytes


def _hicon_to_pil_image(
    hicon: int,
    user32: object,
    gdi32: object,
) -> Image.Image | None:
    """Convert a Win32 ``HICON`` handle to a PIL RGBA image.

    Allocates a 32-bit-per-pixel DIB sized to the icon's colour bitmap,
    fills it via ``GetDIBits``, then wraps the BGRA buffer in a PIL
    image. Returns ``None`` when the icon has a zero-sized colour
    bitmap (monochrome / corrupt icons — rare but observed for
    anti-cheat exes), letting the caller fall back gracefully.

    Callers own the ``HICON`` lifetime — this helper *does not*
    ``DestroyIcon`` so the bitmap can be inspected for diagnostic
    reasons after a decode failure.
    """
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

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

    info = ICONINFO()
    if not user32.GetIconInfo(hicon, ctypes.byref(info)):  # type: ignore[attr-defined]
        return None

    bmp = BITMAP()
    gdi32.GetObjectW(info.hbmColor, ctypes.sizeof(BITMAP), ctypes.byref(bmp))  # type: ignore[attr-defined]
    width = int(bmp.bmWidth)
    height = int(bmp.bmHeight)
    if width <= 0 or height <= 0:
        gdi32.DeleteObject(info.hbmColor)  # type: ignore[attr-defined]
        gdi32.DeleteObject(info.hbmMask)  # type: ignore[attr-defined]
        return None

    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = -height  # top-down DIB so the buffer is row-major
    header.biPlanes = 1
    header.biBitCount = 32
    header.biCompression = 0

    buffer_size = width * height * 4
    buffer = (ctypes.c_byte * buffer_size)()

    hdc = user32.GetDC(0)  # type: ignore[attr-defined]
    try:
        gdi32.GetDIBits(  # type: ignore[attr-defined]
            hdc,
            info.hbmColor,
            0,
            height,
            buffer,
            ctypes.byref(header),
            0,
        )
    finally:
        user32.ReleaseDC(0, hdc)  # type: ignore[attr-defined]
        gdi32.DeleteObject(info.hbmColor)  # type: ignore[attr-defined]
        gdi32.DeleteObject(info.hbmMask)  # type: ignore[attr-defined]

    try:
        return Image.frombuffer(
            "RGBA",
            (width, height),
            bytes(buffer),
            "raw",
            "BGRA",
            0,
            1,
        )
    except (ValueError, OSError):
        return None


def _render_initials_png(app_name: str) -> bytes:
    """Render the deterministic two-letter fallback tile as PNG bytes.

    Same input → same output, byte-for-byte: callers can rely on that
    for HTTP ``ETag`` / ``If-None-Match`` strategies later without
    needing per-row metadata.
    """
    background = _hue_from_name(app_name)
    initials = _initials(app_name)

    image = Image.new("RGB", (_TILE_SIZE, _TILE_SIZE), background)
    draw = ImageDraw.Draw(image)
    font = _load_font(size=30)

    # Pillow ≥10 returns a (left, top, right, bottom) bbox. Centre the
    # glyph on that bbox rather than ``font.getsize`` (removed in 10).
    bbox = draw.textbbox((0, 0), initials, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    x = (_TILE_SIZE - text_width) // 2 - bbox[0]
    y = (_TILE_SIZE - text_height) // 2 - bbox[1]
    draw.text((x, y), initials, fill=_TEXT_RGB, font=font)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _load_font(*, size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    """Best-effort bold TrueType, falling back to PIL's bundled bitmap font.

    The bitmap default is much smaller than ``size`` pixels — visually
    weaker but the tile is still recognisable. We never raise here: a
    missing font on a stripped container should not 500 the route.
    """
    for candidate in (
        "DejaVuSans-Bold.ttf",
        "Arial Bold.ttf",
        "arialbd.ttf",
        "Helvetica-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _normalise_key(app_name: str) -> str:
    """Trim and lowercase the cache key.

    ``app_name`` reaches us from URL paths and template variables, so we
    keep the normalisation minimal: callers that want exe-suffix stripping
    can pre-process. We do *not* drop ``.exe`` here — ``chrome.exe`` and a
    hypothetical ``chrome`` plugin should not collide.
    """
    return (app_name or "").strip().lower()


def _initials(app_name: str) -> str:
    """First two letters of ``app_name``, uppercase. Always exactly 2 chars.

    Empty / single-character / non-alphanumeric inputs degrade
    gracefully so the renderer never has to worry about glyph width:

    * empty / unusable → ``"??"``
    * single useful char → that char + a question mark
    """
    cleaned = "".join(ch for ch in app_name if ch.isalnum())
    if not cleaned:
        return "??"
    if len(cleaned) == 1:
        return (cleaned[0] + "?").upper()
    return cleaned[:2].upper()


def _hue_from_name(app_name: str) -> tuple[int, int, int]:
    """Deterministic RGB background from a stable hash of ``app_name``.

    Uses SHA-256 (not Python's salted ``hash()``) so the result is
    process- and OS-independent — the same app gets the same colour
    across machines and reboots, which matters for backups and shared
    screenshots.
    """
    digest = hashlib.sha256(app_name.encode("utf-8")).hexdigest()
    # First six hex chars → integer in [0, 0xFFFFFF], scaled to [0, 1).
    hue_int = int(digest[:6], 16)
    hue = (hue_int % 360) / 360.0
    return _hsl_to_rgb(hue, _SAT, _LUM)


def _hsl_to_rgb(h: float, s: float, lightness: float) -> tuple[int, int, int]:
    """Standard HSL→RGB conversion. ``h``, ``s``, ``lightness`` in [0,1].

    Implemented inline rather than depending on ``colorsys`` only to keep
    the function self-documenting in this file — the maths is short and
    a one-liner reader does not have to context-switch.
    """
    if s == 0.0:
        gray = round(lightness * 255)
        return (gray, gray, gray)
    q = lightness * (1 + s) if lightness < 0.5 else lightness + s - lightness * s
    p = 2 * lightness - q
    r = _hue_to_channel(p, q, h + 1 / 3)
    g = _hue_to_channel(p, q, h)
    b = _hue_to_channel(p, q, h - 1 / 3)
    return (round(r * 255), round(g * 255), round(b * 255))


def _hue_to_channel(p: float, q: float, t: float) -> float:
    """HSL helper — map a phase ``t`` plus rails ``p``/``q`` to a channel."""
    if t < 0:
        t += 1
    if t > 1:
        t -= 1
    if t < 1 / 6:
        return p + (q - p) * 6 * t
    if t < 1 / 2:
        return q
    if t < 2 / 3:
        return p + (q - p) * (2 / 3 - t) * 6
    return p


__all__ = [
    "get_icon_png",
    "get_icon_source",
    "invalidate",
    "list_known_icons",
    "store_user_icon",
]
