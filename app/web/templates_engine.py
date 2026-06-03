"""Jinja2 environment shared across route modules."""

from __future__ import annotations

import re
import sqlite3
from contextvars import ContextVar
from html import escape as _html_escape
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app.app_aliases import resolve as _resolve_app_alias
from app.logging_setup import get_logger
from app.settings import get_settings

if TYPE_CHECKING:
    from datetime import datetime

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Whitelist for the ``theme`` kv_settings row. Anything outside this set
# is normalised to ``"dark"`` so a corrupt DB never breaks ``base.html``.
_THEME_VALUES: frozenset[str] = frozenset({"dark", "light", "auto"})
_THEME_DEFAULT = "dark"

# Whitelist for the ``compact_mode`` kv_settings row (v0.61). The route
# writes literal ``"1"`` / ``"0"`` strings; anything else collapses to
# ``"0"`` so the body attribute stays well-formed even after a manual
# kv edit.
_COMPACT_VALUES: frozenset[str] = frozenset({"0", "1"})
_COMPACT_DEFAULT = "0"

# Whitelist for the ``grayscale_mode`` kv_settings row (v0.78). Same
# shape as ``compact_mode`` above — anything outside ``{"0", "1"}``
# collapses to the safe default so a manual kv edit can never inject a
# malformed value into ``<body data-grayscale="…">``.
_GRAYSCALE_VALUES: frozenset[str] = frozenset({"0", "1"})
_GRAYSCALE_DEFAULT = "0"

# Whitelist for the ``reduce_motion`` kv_settings row (v0.93). Same
# shape as ``compact_mode`` / ``grayscale_mode`` above — anything outside
# ``{"0", "1"}`` collapses to the safe default so a manual kv edit can
# never inject a malformed value into ``<body data-reduce-motion="…">``.
_REDUCE_MOTION_VALUES: frozenset[str] = frozenset({"0", "1"})
_REDUCE_MOTION_DEFAULT = "0"

# Per-request cache. Templates can call ``get_theme()`` multiple times
# (header, body class, inline script) and we don't want to hit SQLite
# once per call. The contextvar resets implicitly between requests
# because Starlette runs each request in its own ``asyncio.Task`` — and
# ``ContextVar`` values do not leak across tasks.
_theme_cache: ContextVar[str | None] = ContextVar("persona_theme_cache", default=None)
_compact_cache: ContextVar[str | None] = ContextVar("persona_compact_cache", default=None)
_grayscale_cache: ContextVar[str | None] = ContextVar("persona_grayscale_cache", default=None)
_reduce_motion_cache: ContextVar[str | None] = ContextVar(
    "persona_reduce_motion_cache", default=None
)

_compact_log = get_logger("persona.compact")
_grayscale_log = get_logger("persona.grayscale")
_reduce_motion_log = get_logger("persona.reduce_motion")
_linkify_log = get_logger("persona.linkify")

# v0.83 feature 2/3 — OCR URL detection.
# When OCR text is rendered on the screenshot detail page, any
# ``http://`` / ``https://`` substring should become a real anchor so
# operators can jump straight to the captured URL instead of copy-paste.
# The pattern is intentionally permissive (``\S+``) so query strings,
# fragments, and unicode-rich paths all survive — trailing punctuation
# like a sentence-terminating ``.`` or ``)`` is peeled off after the
# match so we don't generate dead links. Output is HTML-escaped per
# segment, then concatenated and wrapped in :class:`markupsafe.Markup`
# so the template's ``|safe`` is honoured without disabling Jinja's
# auto-escaping for the surrounding context.
_URL_PATTERN: re.Pattern[str] = re.compile(r"https?://\S+")
# Trailing punctuation that should be peeled off a matched URL and
# pushed back into the plaintext segment. Includes ASCII sentence
# punctuation plus the common typographic closing quotes that OCR
# engines emit. Stored as a frozenset of single chars (not a string
# literal) to keep ``ruff`` happy about ambiguous Unicode glyphs.
_URL_TRAILING_PUNCT: frozenset[str] = frozenset(
    [
        ".",
        ",",
        ";",
        ":",
        "!",
        "?",
        ")",
        '"',
        "'",
        "»",  # right-pointing double angle quotation mark
        "”",  # right double quotation mark
        "’",  # noqa: RUF001 — right single quote (OCR-friendly apostrophe)
    ]
)


def _linkify_urls(value: str | None) -> Markup:
    """Wrap ``http(s)://…`` substrings in anchor tags, escape the rest.

    Registered as a Jinja2 filter. The non-URL segments pass through
    :func:`html.escape` so user-controlled OCR text can never inject raw
    markup; URLs themselves are also escaped before being written into
    the ``href`` attribute and the visible anchor body. Anchors open in
    a new tab with ``rel="noopener noreferrer"`` so the opened page
    cannot reach back through ``window.opener``.
    """
    if value is None or value == "":
        return Markup("")
    pieces: list[str] = []
    cursor = 0
    match_count = 0
    for match in _URL_PATTERN.finditer(value):
        start, end = match.span()
        if start > cursor:
            pieces.append(_html_escape(value[cursor:start]))
        raw_url = match.group(0)
        # Peel trailing punctuation back into the plaintext so a URL at
        # the end of a sentence doesn't drag the period into ``href``.
        trailing = ""
        while raw_url and raw_url[-1] in _URL_TRAILING_PUNCT:
            trailing = raw_url[-1] + trailing
            raw_url = raw_url[:-1]
        if not raw_url:
            pieces.append(_html_escape(trailing))
            cursor = end
            continue
        safe_url = _html_escape(raw_url, quote=True)
        pieces.append(
            f'<a href="{safe_url}" target="_blank" rel="noopener noreferrer">{safe_url}</a>'
        )
        if trailing:
            pieces.append(_html_escape(trailing))
        cursor = end
        match_count += 1
    if cursor < len(value):
        pieces.append(_html_escape(value[cursor:]))
    if match_count:
        _linkify_log.debug("linkify.matched", urls=match_count, length=len(value))
    # Safe to wrap as Markup: every ``pieces`` entry was produced by
    # ``html.escape`` (plaintext segments + the URL fragments injected
    # into both the ``href`` and the visible anchor body), so the joined
    # string contains no un-escaped user input.
    return Markup("".join(pieces))  # noqa: S704 — all segments are html.escape-d above


def _format_human_time(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _format_human_date(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%Y-%m-%d")


def _format_clock(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.strftime("%H:%M")


def _format_filesize(size_bytes: int | None) -> str:
    if size_bytes is None:
        return "—"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size_bytes)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.1f} {units[idx]}"


def _thumbnail_url(thumbnail_path: str | None) -> str | None:
    """Lazy import to avoid circular import with routes.thumbnails."""
    if thumbnail_path is None:
        return None
    from app.web.routes.thumbnails import thumbnail_url  # noqa: PLC0415 — circular import guard

    return thumbnail_url(thumbnail_path)


def _read_theme_from_db() -> str:
    """Synchronous read of the ``theme`` row from ``kv_settings``.

    Called from inside Jinja, which is synchronous — we cannot ``await``
    the aiosqlite pool here. A short-lived stdlib ``sqlite3`` connection
    against the same file is safe because SQLite's WAL mode permits
    concurrent readers alongside the async writers.

    Any failure (missing DB, missing row, corrupt value) falls back to
    the default ``"dark"`` rather than raising — a template render must
    never 500 because of a theme lookup.
    """
    db_path = get_settings().db_path
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.execute(
                "SELECT value FROM kv_settings WHERE key = ?",
                ("theme",),
            )
            row = cursor.fetchone()
    except sqlite3.Error:
        return _THEME_DEFAULT
    if row is None:
        return _THEME_DEFAULT
    value = str(row[0])
    if value not in _THEME_VALUES:
        return _THEME_DEFAULT
    return value


def get_theme() -> str:
    """Return the stored theme (``dark`` / ``light`` / ``auto``).

    Registered as a Jinja global so templates can call ``{{ get_theme() }}``
    directly without every route having to pass it through the context.
    The first call inside a request hits SQLite; subsequent calls in the
    same request reuse the cached value via a :class:`~contextvars.ContextVar`.
    """
    cached = _theme_cache.get()
    if cached is not None:
        return cached
    value = _read_theme_from_db()
    _theme_cache.set(value)
    return value


def invalidate_theme_cache() -> None:
    """Drop the per-request theme cache after the kv row is rewritten.

    Called from the POST handler in :mod:`app.web.routes.theme` so the
    redirect-then-render that follows a save reflects the new value
    rather than the just-overwritten one cached earlier in the same
    request.
    """
    _theme_cache.set(None)


def _read_compact_from_db() -> str:
    """Synchronous read of the ``compact_mode`` row from ``kv_settings``.

    Mirrors :func:`_read_theme_from_db` — Jinja globals run synchronously
    so the aiosqlite pool is off-limits; a short stdlib ``sqlite3``
    reader against the WAL-mode database is safe alongside the async
    writers. Any failure (missing DB / row / bogus value) falls back to
    ``"0"`` so a template render never 500s because of this lookup.
    """
    db_path = get_settings().db_path
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.execute(
                "SELECT value FROM kv_settings WHERE key = ?",
                ("compact_mode",),
            )
            row = cursor.fetchone()
    except sqlite3.Error as exc:
        _compact_log.debug("compact.read.error", error=str(exc))
        return _COMPACT_DEFAULT
    if row is None:
        return _COMPACT_DEFAULT
    value = str(row[0]).strip()
    if value not in _COMPACT_VALUES:
        return _COMPACT_DEFAULT
    return value


def get_compact_mode() -> str:
    """Return ``"1"`` when compact mode is on, ``"0"`` otherwise.

    Registered as a Jinja global so :file:`base.html` can stamp the
    value straight onto ``<body data-compact="...">``. The first call
    inside a request hits SQLite; later calls in the same request reuse
    the per-request :class:`~contextvars.ContextVar` cache.
    """
    cached = _compact_cache.get()
    if cached is not None:
        return cached
    value = _read_compact_from_db()
    _compact_cache.set(value)
    return value


def invalidate_compact_cache() -> None:
    """Drop the per-request compact-mode cache after the kv row is rewritten.

    Called from the POST handler in :mod:`app.web.routes.settings` so
    the redirect-then-render that follows a save reflects the new value
    rather than the just-overwritten one cached earlier in the same
    request.
    """
    _compact_cache.set(None)


def _read_grayscale_from_db() -> str:
    """Synchronous read of the ``grayscale_mode`` row from ``kv_settings``.

    Mirrors :func:`_read_compact_from_db` — Jinja globals run
    synchronously so the aiosqlite pool is off-limits; a short stdlib
    ``sqlite3`` reader against the WAL-mode database is safe alongside
    the async writers. Any failure (missing DB / row / bogus value)
    falls back to ``"0"`` so a template render never 500s because of
    this lookup.
    """
    db_path = get_settings().db_path
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.execute(
                "SELECT value FROM kv_settings WHERE key = ?",
                ("grayscale_mode",),
            )
            row = cursor.fetchone()
    except sqlite3.Error as exc:
        _grayscale_log.debug("grayscale.read.error", error=str(exc))
        return _GRAYSCALE_DEFAULT
    if row is None:
        return _GRAYSCALE_DEFAULT
    value = str(row[0]).strip()
    if value not in _GRAYSCALE_VALUES:
        return _GRAYSCALE_DEFAULT
    return value


def get_grayscale_mode() -> str:
    """Return ``"1"`` when grayscale mode is on, ``"0"`` otherwise.

    Registered as a Jinja global so :file:`base.html` can stamp the
    value straight onto ``<body data-grayscale="...">``. The first call
    inside a request hits SQLite; later calls in the same request reuse
    the per-request :class:`~contextvars.ContextVar` cache.
    """
    cached = _grayscale_cache.get()
    if cached is not None:
        return cached
    value = _read_grayscale_from_db()
    _grayscale_cache.set(value)
    return value


def invalidate_grayscale_cache() -> None:
    """Drop the per-request grayscale-mode cache after the kv row is rewritten.

    Called from the POST handler in :mod:`app.web.routes.settings` so
    the redirect-then-render that follows a save reflects the new value
    rather than the just-overwritten one cached earlier in the same
    request.
    """
    _grayscale_cache.set(None)


def _read_reduce_motion_from_db() -> str:
    """Synchronous read of the ``reduce_motion`` row from ``kv_settings``.

    Mirrors :func:`_read_grayscale_from_db` — Jinja globals run
    synchronously so the aiosqlite pool is off-limits; a short stdlib
    ``sqlite3`` reader against the WAL-mode database is safe alongside
    the async writers. Any failure (missing DB / row / bogus value)
    falls back to ``"0"`` so a template render never 500s because of
    this lookup.
    """
    db_path = get_settings().db_path
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.execute(
                "SELECT value FROM kv_settings WHERE key = ?",
                ("reduce_motion",),
            )
            row = cursor.fetchone()
    except sqlite3.Error as exc:
        _reduce_motion_log.debug("reduce_motion.read.error", error=str(exc))
        return _REDUCE_MOTION_DEFAULT
    if row is None:
        return _REDUCE_MOTION_DEFAULT
    value = str(row[0]).strip()
    if value not in _REDUCE_MOTION_VALUES:
        return _REDUCE_MOTION_DEFAULT
    return value


def get_reduce_motion() -> str:
    """Return ``"1"`` when reduce-motion is on, ``"0"`` otherwise.

    Registered as a Jinja global so :file:`base.html` can stamp the
    value straight onto ``<body data-reduce-motion="...">``. The first
    call inside a request hits SQLite; later calls in the same request
    reuse the per-request :class:`~contextvars.ContextVar` cache.
    """
    cached = _reduce_motion_cache.get()
    if cached is not None:
        return cached
    value = _read_reduce_motion_from_db()
    _reduce_motion_cache.set(value)
    return value


def invalidate_reduce_motion_cache() -> None:
    """Drop the per-request reduce-motion cache after the kv row is rewritten.

    Called from the POST handler in :mod:`app.web.routes.settings` so
    the redirect-then-render that follows a save reflects the new value
    rather than the just-overwritten one cached earlier in the same
    request.
    """
    _reduce_motion_cache.set(None)


templates.env.filters["humantime"] = _format_human_time
templates.env.filters["humandate"] = _format_human_date
templates.env.filters["clock"] = _format_clock
templates.env.filters["filesize"] = _format_filesize
templates.env.filters["thumbnail_url"] = _thumbnail_url
templates.env.filters["app_alias"] = _resolve_app_alias
templates.env.filters["linkify_urls"] = _linkify_urls

templates.env.globals["get_theme"] = get_theme
templates.env.globals["get_compact_mode"] = get_compact_mode
templates.env.globals["get_grayscale_mode"] = get_grayscale_mode
templates.env.globals["get_reduce_motion"] = get_reduce_motion
