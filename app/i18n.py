"""Translation lookup for Jinja templates and route helpers (v1.1).

Persona stores UI strings as ``key -> phrase`` pairs in per-language JSON
files under :mod:`app/translations`. The active language is picked from
the ``ui_language`` row in ``kv_settings`` (migration 081); anything
outside the whitelist :data:`SUPPORTED_LANGUAGES` collapses to the
default ``"ru"`` so a manual kv edit can never wedge the renderer.
Persona — русскоязычный продукт, поэтому язык по умолчанию русский.

The module deliberately mirrors the *synchronous-Jinja-global / async
read elsewhere* split used by the theme, compact-mode, grayscale, and
reduce-motion plumbing in :mod:`app.web.templates_engine`:

* :func:`t` is pure dict access — no I/O, safe to call from inside a
  Jinja template thousands of times per request.
* :func:`get_ui_language` reads ``kv_settings`` via a short-lived stdlib
  ``sqlite3`` connection (the same WAL-safe pattern used by
  :func:`app.web.templates_engine._read_theme_from_db`) and caches the
  result for the duration of the request via a
  :class:`~contextvars.ContextVar`.

Translation files are loaded once at import time. The dict is *frozen*
behind a private cache so repeated calls don't trigger disk reads, and
unknown languages return the English table — so a partially translated
locale still renders rather than 500-ing.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Final

from app.logging_setup import get_logger
from app.settings import get_settings

_log = get_logger("persona.i18n")

# Directory holding ``{lang}.json`` translation tables. Kept relative to
# this file (not the data dir) so the bundle ships with the package —
# translations are source assets, not user data.
TRANSLATIONS_DIR: Final[Path] = Path(__file__).parent / "translations"

# Whitelist for the ``ui_language`` kv row. Adding a new locale means
# dropping ``{code}.json`` into :data:`TRANSLATIONS_DIR` AND adding the
# code here — the dual gate stops typos in kv from silently selecting a
# file that doesn't exist.
SUPPORTED_LANGUAGES: Final[frozenset[str]] = frozenset({"en", "ru", "de"})
# Persona — русскоязычный продукт по умолчанию (kv ui_language всё ещё может
# переключить на en/de). Фолбэк-цепочка в t(): effective → ru → en → key.
DEFAULT_LANGUAGE: Final[str] = "ru"

# kv key shared with :mod:`app.web.routes.settings` (the language
# selector) and migration ``081_ui_language.sql`` — single source of
# truth so a rename can't drift the writer and reader out of sync.
UI_LANGUAGE_KV_KEY: Final[str] = "ui_language"

# Per-request cache. Templates touch ``t(...)`` on nearly every line in
# :file:`base.html`, so resolving the active language from SQLite once
# per request (rather than per call) matters. The contextvar resets
# implicitly between requests because Starlette runs each request in its
# own ``asyncio.Task`` and ``ContextVar`` values do not leak across tasks.
_language_cache: ContextVar[str | None] = ContextVar(
    "persona_ui_language_cache", default=None
)

# Процесс-глобальный TTL-кэш ui_language (перф, 2026-07-02) — см.
# _read_ui_language_from_db. ContextVar выше гасит повторы в одном запросе,
# этот кэш убирает свежий connect МЕЖДУ запросами.
_LANG_CACHE_TTL: Final[float] = 15.0
_lang_proc_cache: tuple[str, float] | None = None


def _set_lang_proc_cache(value: str, expires: float) -> None:
    global _lang_proc_cache
    _lang_proc_cache = (value, expires)


def _load_translations() -> dict[str, dict[str, str]]:
    """Read every ``{lang}.json`` under :data:`TRANSLATIONS_DIR`.

    Called once at import time. A corrupt or unreadable file is logged
    and skipped rather than crashing import — a broken translation file
    should never take the whole app offline. Languages absent from
    :data:`SUPPORTED_LANGUAGES` are loaded anyway (so we can warn) but
    the runtime selector still refuses to switch to them.
    """
    tables: dict[str, dict[str, str]] = {}
    if not TRANSLATIONS_DIR.is_dir():
        _log.warning(
            "i18n.translations_dir_missing",
            path=str(TRANSLATIONS_DIR),
        )
        return tables
    for path in sorted(TRANSLATIONS_DIR.glob("*.json")):
        lang = path.stem
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning(
                "i18n.load_failed",
                lang=lang,
                path=str(path),
                error=str(exc),
            )
            continue
        if not isinstance(data, dict):
            _log.warning("i18n.load_not_dict", lang=lang, path=str(path))
            continue
        # Coerce to a plain ``dict[str, str]`` — non-string values would
        # blow up at ``str.format``-style use sites and a translation
        # table is always flat key->string by design.
        tables[lang] = {str(k): str(v) for k, v in data.items()}
        _log.debug("i18n.loaded", lang=lang, keys=len(tables[lang]))
    return tables


# Frozen at import time. Re-reading on every ``t()`` call would dominate
# the cost of templating; translation files don't change at runtime.
_TRANSLATIONS: Final[dict[str, dict[str, str]]] = _load_translations()

# v1.10 fix 1/3 — translation coverage expanded from ~80 nav-only keys
# to ~200+ keys covering titles, table headers, sort options, counts,
# buttons, filter prompts, day-nav (Today/Prev/Next), empty states, and
# subtitles across timeline / search / dashboard / heatmap / hours /
# streak / notes_day / screenshot / welcome / settings.
_log.info(
    "i18n.expanded",
    languages=sorted(_TRANSLATIONS.keys()),
    key_counts={lang: len(table) for lang, table in _TRANSLATIONS.items()},
)


def t(key: str, lang: str | None = None) -> str:
    """Return the translation of ``key`` in ``lang``.

    Resolution order:

    1. ``lang`` table (if loaded and the key exists).
    2. The English fallback table (so partial translations still render
       a real word rather than a debug-looking key).
    3. ``key`` itself — the absolute last resort, surfaced verbatim so
       missing translations are visible in QA rather than silently empty.

    The Jinja global registered in :mod:`app.web.templates_engine` binds
    ``lang`` to the active ``ui_language`` setting; route code can call
    ``t("save", lang="ru")`` directly when it needs a translated string
    for a flash message or a JSON response.
    """
    effective_lang = lang if lang in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE
    table = _TRANSLATIONS.get(effective_lang, {})
    value = table.get(key)
    if value is not None:
        return value
    # Цепочка фолбэков: язык по умолчанию (ru) → английский → сам ключ.
    # Так отсутствующий ключ всё равно отрендерит реальное слово, а не id,
    # и ни ru-, ни de-пользователь не увидит «голый» идентификатор.
    for fb in (DEFAULT_LANGUAGE, "en"):
        if fb != effective_lang:
            fallback = _TRANSLATIONS.get(fb, {}).get(key)
            if fallback is not None:
                return fallback
    return key


def _read_ui_language_from_db() -> str:
    """Synchronous read of the ``ui_language`` row from ``kv_settings``.

    Mirrors :func:`app.web.templates_engine._read_theme_from_db` — Jinja
    globals run synchronously so the aiosqlite pool is off-limits; a
    short stdlib ``sqlite3`` reader against the WAL-mode database is
    safe alongside the async writers. Any failure (missing DB, missing
    row, language outside :data:`SUPPORTED_LANGUAGES`) falls back to
    :data:`DEFAULT_LANGUAGE` so a template render never 500s because of
    this lookup.
    """
    # Процесс-глобальный TTL-кэш (перф, 2026-07-02): раньше открывали свежий
    # sqlite3.connect на КАЖДЫЙ рендер (~30-80ms блокировки event-loop). Язык
    # меняется раз в месяц; при сохранении invalidate_language_cache() сбросит
    # и этот кэш → редирект-после-сохранения покажет новый язык сразу.
    now = time.monotonic()
    if _lang_proc_cache and now < _lang_proc_cache[1]:
        return _lang_proc_cache[0]
    db_path = get_settings().db_path
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.execute(
                "SELECT value FROM kv_settings WHERE key = ?",
                (UI_LANGUAGE_KV_KEY,),
            )
            row = cursor.fetchone()
    except sqlite3.Error as exc:
        _log.debug("i18n.read.error", error=str(exc))
        return _lang_proc_cache[0] if _lang_proc_cache else DEFAULT_LANGUAGE
    if row is None:
        value = DEFAULT_LANGUAGE
    else:
        value = str(row[0]).strip()
        if value not in SUPPORTED_LANGUAGES:
            value = DEFAULT_LANGUAGE
    _set_lang_proc_cache(value, now + _LANG_CACHE_TTL)
    return value


def get_ui_language() -> str:
    """Return the active UI language code (``"en"`` / ``"ru"`` / …).

    Registered as a Jinja global so :file:`base.html` can stamp the
    value straight onto ``<html lang="…">`` and the bound ``t()`` global
    can pick it up without each route having to thread the value through
    the template context. The first call inside a request hits SQLite;
    later calls in the same request reuse the per-request
    :class:`~contextvars.ContextVar` cache.
    """
    cached = _language_cache.get()
    if cached is not None:
        return cached
    value = _read_ui_language_from_db()
    _language_cache.set(value)
    return value


def invalidate_language_cache() -> None:
    """Drop the per-request language cache after the kv row is rewritten.

    Called from the POST handler in :mod:`app.web.routes.settings` so
    the redirect-target render reflects the new value rather than the
    value cached earlier in this same request when the GET form was
    rendered.
    """
    global _lang_proc_cache
    _language_cache.set(None)
    _lang_proc_cache = None


__all__ = [
    "DEFAULT_LANGUAGE",
    "SUPPORTED_LANGUAGES",
    "TRANSLATIONS_DIR",
    "UI_LANGUAGE_KV_KEY",
    "get_ui_language",
    "invalidate_language_cache",
    "t",
]
