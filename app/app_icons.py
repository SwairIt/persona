"""Per-app icon cache — DB-backed, deterministic, cross-platform-friendly.

Every screenshot row carries an ``app_name`` (Win32 window-class or
process-executable name — ``Slack``, ``chrome.exe``, ``devenv.exe``).
The UI wants a small icon next to that string in the timeline / tags
chips. This module is the cache layer that backs ``/app-icon/{name}.png``:

1. Lookup by ``app_name`` in the ``app_icon`` SQLite table
   (see :mod:`app.storage.migrations.044_app_icons`).
2. On Windows, opportunistically try to extract the real exe icon via
   the existing :mod:`app.capture.icons` Shell32 path. The attempt is
   gated by ``PERSONA_APP_ICONS_USE_SHELL32`` (default off) because on
   a busy host with thousands of processes ``psutil.process_iter`` can
   tie up the request thread for tens of seconds — fine for a
   background backfill job, not fine for a hot timeline render.
3. Otherwise (the default path), generate a deterministic "initials"
   PNG: 64x64 RGB tile, background hue derived from a stable SHA-256
   of the lowercased ``app_name``, centred uppercase first-two letters
   in white. Stored with ``source='initials'``.

The generator is intentionally deterministic so the *same* app gets the
*same* tile across reinstalls, browser refreshes and across machines.

All PIL work runs inside :func:`anyio.to_thread.run_sync` so the calling
coroutine never blocks the event loop on disk IO or pixel arithmetic.
"""

from __future__ import annotations

import hashlib
import io
import os
import sys
import time
from typing import Final

import anyio
from PIL import Image, ImageDraw, ImageFont

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.app_icons")

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
       ``source='shell32'`` (Windows real-exe extraction) or
       ``source='initials'`` (deterministic fallback). All three are
       returned as-is — *the user override takes precedence simply by
       virtue of being the row that's there*, because the upload path
       writes it with ``ON CONFLICT DO UPDATE`` and the reset path
       deletes the row so the next read falls through to (2).
    2. On miss, generate (Shell32 attempt → initials fallback) and
       persist with the appropriate non-user ``source``.

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
    ``source`` (one of ``shell32`` / ``initials`` / ``user``) so the
    template can flag custom overrides without a second query.
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

    On Windows with the ``PERSONA_APP_ICONS_USE_SHELL32`` opt-in env var
    set we try the Shell32 path under an observed wall-clock budget; any
    miss or failure transparently falls through to the deterministic
    initials tile so a slow / unavailable extractor never propagates a
    user-visible error.
    """
    if sys.platform == "win32" and _shell32_opted_in():
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
