"""Configurable Tesseract OCR language list.

Persona stores the user's preferred Tesseract languages as a ``+``-joined
string in the ``kv_settings`` table under the ``ocr_languages`` key
(matching the syntax ``pytesseract.image_to_string(..., lang=...)`` expects,
e.g. ``"eng+rus"``).

Three helpers are exposed:

* :func:`get_installed_languages` - probes Tesseract for every language
  pack that is actually present on disk. Falls back to ``["eng"]`` if
  Tesseract or its binary is unavailable, so the settings page never
  crashes on machines without the OCR stack installed.
* :func:`get_configured_languages` - returns the user's current
  selection as a list (``["eng", "rus"]``).
* :func:`set_configured_languages` - validates each candidate against
  the installed list and persists ``+``-joined back to ``kv_settings``.

A sync helper, :func:`get_ocr_lang_string`, returns the raw
``+``-joined string suitable for ``pytesseract``'s ``lang`` argument
without touching the database - the OCR worker uses it together with
:func:`refresh_ocr_lang_string` to keep a short TTL cache hot.
"""

from __future__ import annotations

import asyncio
import time
from typing import Final

import pytesseract

from app.logging_setup import get_logger
from app.ocr.tesseract import is_available
from app.settings import get_settings
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv

logger = get_logger("persona.ocr.languages")

_KV_KEY: Final[str] = "ocr_languages"
_DEFAULT_LANGS: Final[tuple[str, ...]] = ("eng",)

# Cache the joined string in-process so the OCR worker does not hammer
# SQLite on every screenshot. ``_CACHE_TTL_SECONDS`` is intentionally short
# enough that UI changes propagate quickly without per-shot DB churn.
_CACHE_TTL_SECONDS: Final[float] = 60.0
_cache_value: str | None = None
_cache_expires_at: float = 0.0
_cache_lock = asyncio.Lock()


def _split(value: str) -> list[str]:
    """Split a ``+``-joined Tesseract language string into a clean list."""
    return [part.strip() for part in value.split("+") if part.strip()]


def _join(langs: list[str]) -> str:
    """Join a Tesseract language list using the ``+`` syntax pytesseract expects."""
    return "+".join(langs)


async def get_installed_languages() -> list[str]:
    """Return every Tesseract language pack present on disk.

    Falls back to ``["eng"]`` on any error (missing binary, missing
    pytesseract, broken language-pack directory). The settings UI relies
    on a non-empty list to render its checkbox grid.
    """
    settings = get_settings()
    if not is_available(settings.tesseract_path):
        logger.info("ocr.languages.installed.fallback", reason="tesseract_unavailable")
        return list(_DEFAULT_LANGS)

    def _probe() -> list[str]:
        if settings.tesseract_path is not None:
            pytesseract.pytesseract.tesseract_cmd = str(settings.tesseract_path)
        raw = pytesseract.get_languages(config="")
        return [str(item) for item in raw if str(item).strip()]

    try:
        languages = await asyncio.to_thread(_probe)
    except Exception as exc:
        logger.warning("ocr.languages.installed.failed", error=str(exc))
        return list(_DEFAULT_LANGS)

    if not languages:
        return list(_DEFAULT_LANGS)

    languages.sort()
    return languages


async def get_configured_languages() -> list[str]:
    """Return the user's currently selected Tesseract languages.

    Reads the ``ocr_languages`` key from ``kv_settings``. If the row is
    missing or empty, returns the default (``["eng"]``).
    """
    async with get_connection() as conn:
        raw = await get_kv(conn, _KV_KEY)
    if raw is None:
        return list(_DEFAULT_LANGS)
    parts = _split(raw)
    if not parts:
        return list(_DEFAULT_LANGS)
    return parts


async def set_configured_languages(langs: list[str]) -> None:
    """Validate and persist the user's Tesseract language selection.

    Each language must be present in :func:`get_installed_languages`.
    Empty selections and duplicates are rejected so the UI never silently
    drops the user's choice. Successful writes invalidate the in-process
    cache used by the OCR worker.
    """
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in langs:
        candidate = raw.strip()
        if not candidate:
            continue
        if candidate in seen:
            continue
        cleaned.append(candidate)
        seen.add(candidate)

    if not cleaned:
        msg = "At least one language must be selected"
        raise ValueError(msg)

    installed = set(await get_installed_languages())
    unknown = [lang for lang in cleaned if lang not in installed]
    if unknown:
        msg = f"Unknown Tesseract language(s): {', '.join(unknown)}"
        raise ValueError(msg)

    joined = _join(cleaned)
    async with get_connection() as conn:
        await set_kv(conn, _KV_KEY, joined)

    await invalidate_cache()
    logger.info("ocr.languages.updated", langs=cleaned, joined=joined)


def get_ocr_lang_string() -> str:
    """Return the cached ``+``-joined language string for ``pytesseract``.

    This is a synchronous read of the in-process cache populated by
    :func:`refresh_ocr_lang_string`. When the cache is cold (first call,
    or after :func:`invalidate_cache`), returns the default ``"eng"`` -
    callers that care about freshness must call
    :func:`refresh_ocr_lang_string` from an async context first.
    """
    if _cache_value is None:
        return _join(list(_DEFAULT_LANGS))
    return _cache_value


async def refresh_ocr_lang_string(*, force: bool = False) -> str:
    """Refresh the in-process language-string cache when its TTL has expired.

    Returns the current cached value. Pass ``force=True`` to bypass the
    TTL and re-read from SQLite immediately - useful right after the
    settings page POSTs an update.
    """
    global _cache_value, _cache_expires_at  # noqa: PLW0603

    now = time.monotonic()
    if not force and _cache_value is not None and now < _cache_expires_at:
        return _cache_value

    async with _cache_lock:
        # Re-check inside the lock to avoid a thundering-herd refresh.
        now = time.monotonic()
        if not force and _cache_value is not None and now < _cache_expires_at:
            return _cache_value

        configured = await get_configured_languages()
        joined = _join(configured)
        _cache_value = joined
        _cache_expires_at = now + _CACHE_TTL_SECONDS
        return joined


async def invalidate_cache() -> None:
    """Drop the in-process cache so the next refresh re-reads SQLite."""
    global _cache_value, _cache_expires_at  # noqa: PLW0603

    async with _cache_lock:
        _cache_value = None
        _cache_expires_at = 0.0


__all__ = [
    "get_configured_languages",
    "get_installed_languages",
    "get_ocr_lang_string",
    "invalidate_cache",
    "refresh_ocr_lang_string",
    "set_configured_languages",
]
