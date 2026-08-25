"""Jinja2 environment shared across route modules."""

from __future__ import annotations

import re
import sqlite3
import time
from contextvars import ContextVar
from datetime import datetime, timezone, tzinfo
from html import escape as _html_escape
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app import __version__ as _app_version
from app.app_aliases import resolve as _resolve_app_alias
from app.i18n import get_ui_language as _get_ui_language
from app.i18n import t as _translate
from app.logging_setup import get_logger
from app.request_ctx import get_member_uid
from app.settings import get_settings

TEMPLATES_DIR = Path(__file__).parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Whitelist for the ``theme`` kv_settings row. Anything outside this set
# is normalised to ``"dark"`` so a corrupt DB never breaks ``base.html``.
_THEME_VALUES: frozenset[str] = frozenset({"dark", "light", "auto", "persona", "cosmos", "cosmos-dark"})
_THEME_DEFAULT = "persona"

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

# ── Процесс-глобальный TTL-кэш kv-значений (перф, 2026-07-02) ──────────────
# Jinja синхронна → нельзя await'ить aiosqlite-пул, поэтому UI-геттеры темы/
# языка/флагов и фильтр |localtime читали настройки СВОИМ ``sqlite3.connect``
# на КАЖДЫЙ рендер: до ~8 открытий на страницу, а |localtime — по одному НА
# КАЖДУЮ карточку (до 500 на таймлайне). На Windows/WAL открытие соединения
# стоит ~30-80ms и БЛОКИРУЕТ event-loop → это была основная доля TTFB
# («сайт плохо грузит»). ContextVar-кэши ниже гасят повторы В ОДНОМ запросе,
# но между запросами всё равно шёл свежий connect. Этот процесс-глобальный
# кэш с коротким TTL схлопывает почти все чтения в ноль дисковых обращений.
# Настройки UI меняются раз в месяц; для ключей с invalidate_*_cache кэш
# сбрасывается сразу при сохранении, для остальных достаточно TTL.
_KV_CACHE_TTL = 15.0
_kv_value_cache: dict[str, tuple[str | None, float]] = {}


def _cached_kv_value(key: str, ttl: float = _KV_CACHE_TTL) -> str | None:
    """Сырое значение строки ``kv_settings`` с процесс-глобальным TTL-кэшем.

    Возвращает строку или ``None`` (строки нет / БД недоступна). Нормализацию
    и дефолты применяет вызывающий геттер — семантика 1:1 со старым чтением.
    При ошибке БД отдаём последнее известное значение (или ``None``), чтобы
    рендер никогда не падал. Операции с dict атомарны под GIL; гонка максимум
    приведёт к лишнему безвредному connect, поэтому лока нет.
    """
    now = time.monotonic()
    cached = _kv_value_cache.get(key)
    if cached is not None and now < cached[1]:
        return cached[0]
    db_path = get_settings().db_path
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.execute(
                "SELECT value FROM kv_settings WHERE key = ?", (key,)
            )
            row = cursor.fetchone()
    except sqlite3.Error:
        return cached[0] if cached is not None else None
    value = None if row is None else str(row[0])
    _kv_value_cache[key] = (value, now + ttl)
    return value


def _invalidate_kv_value(key: str) -> None:
    """Сбросить процесс-кэш одной kv-строки (после записи настройки)."""
    _kv_value_cache.pop(key, None)


# ── Тот же TTL-кэш, но для per-user настроек (таблица ``user_settings``) ────
# Отдельный dict, а НЕ общий с ``_kv_value_cache``: ключ здесь составной
# ``(user_id, key)``. Складывать их в один словарь по строковому ``key``
# нельзя ни в каком виде — тема/язык одного пользователя утекли бы другому
# (и глобальному kv). Кортеж-ключ делает коллизию между пользователями
# структурно невозможной.
_user_kv_value_cache: dict[tuple[int, str], tuple[str | None, float]] = {}


def get_user_kv_sync(user_id: int, key: str, ttl: float = _KV_CACHE_TTL) -> str | None:
    """Сырое значение строки ``user_settings`` с процесс-глобальным TTL-кэшем.

    Синхронный близнец :func:`app.storage.repository.get_user_kv` — Jinja
    синхронна, поэтому aiosqlite-пул недоступен, и читаем коротким stdlib
    ``sqlite3`` по тому же пути к БД, что и :func:`_cached_kv_value`.
    Возвращает строку или ``None`` (строки нет / БД недоступна / таблицы
    ещё нет). При ошибке БД отдаём последнее известное значение, чтобы
    рендер никогда не падал.
    """
    now = time.monotonic()
    cache_key = (user_id, key)
    cached = _user_kv_value_cache.get(cache_key)
    if cached is not None and now < cached[1]:
        return cached[0]
    db_path = get_settings().db_path
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.execute(
                "SELECT value FROM user_settings WHERE user_id = ? AND key = ?",
                (user_id, key),
            )
            row = cursor.fetchone()
    except sqlite3.Error:
        return cached[0] if cached is not None else None
    value = None if row is None else str(row[0])
    _user_kv_value_cache[cache_key] = (value, now + ttl)
    return value


def invalidate_user_kv_sync(user_id: int, key: str) -> None:
    """Сбросить процесс-кэш одной ``user_settings``-строки (после записи)."""
    _user_kv_value_cache.pop((user_id, key), None)


_compact_log = get_logger("persona.compact")
_grayscale_log = get_logger("persona.grayscale")
_reduce_motion_log = get_logger("persona.reduce_motion")
_linkify_log = get_logger("persona.linkify")
# v1.10 fix 2/3 — timezone-aware rendering of ``captured_at``.
# Captures land in SQLite as ISO-8601 UTC strings (see
# :func:`app.storage.time.iso`). The legacy ``_format_clock`` /
# ``_format_human_time`` filters just ``strftime``'d the raw value, so a
# user in MSK (UTC+3) saw a 21:19 capture rendered as 18:19. The new
# ``localtime`` filter resolves the display timezone (kv_settings row
# ``display_timezone`` if set; otherwise the process-local zone) and
# converts before formatting. The logger is named ``persona.tz_fix`` so
# the bug fix is greppable across structured-log pipelines.
_tz_log = get_logger("persona.tz_fix")

# Format flags for :func:`_format_localtime`. ``"short"`` is the default
# (matches the legacy ``|clock`` rendering used on the timeline cards),
# ``"full"`` mirrors the legacy ``|humantime`` minus the seconds
# component which is noise at minute-grained capture rates, and
# ``"date"`` covers the ``back to YYYY-MM-DD`` / day-navigation links
# that used to call ``shot.captured_at.strftime('%Y-%m-%d')`` directly
# and therefore rendered the *UTC* date — visibly wrong around midnight
# local time.
_LOCALTIME_FORMATS: dict[str, str] = {
    "short": "%H:%M",
    "full": "%Y-%m-%d %H:%M",
    "date": "%Y-%m-%d",
}
_LOCALTIME_DEFAULT = "short"

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


def _read_display_timezone_from_db() -> str:
    """Synchronous read of the ``display_timezone`` row from ``kv_settings``.

    Mirrors :func:`_read_theme_from_db` — Jinja filters run synchronously
    so the aiosqlite pool is off-limits; a short stdlib ``sqlite3``
    reader against the WAL-mode database is safe alongside the async
    writers. Any failure (missing DB / missing row / SQLite error) falls
    back to ``""`` so the ``localtime`` filter degrades to the process-
    local zone rather than 500-ing the page.
    """
    value = _cached_kv_value("display_timezone")
    return "" if value is None else value.strip()


def resolve_display_tz() -> tzinfo:
    """Return the :class:`tzinfo` to use for rendering captured_at.

    Reads the operator-controlled ``display_timezone`` kv row; if set to
    an IANA name (e.g. ``"Europe/Moscow"``) we hand back a
    :class:`zoneinfo.ZoneInfo`. Empty string or an unknown name falls
    back to the process-local zone via
    ``datetime.now().astimezone().tzinfo`` — guaranteed non-``None`` on
    Python 3.12, matches the ``_today`` helper in
    :mod:`app.web.routes.timeline`.
    """
    tz_name = _read_display_timezone_from_db()
    if tz_name:
        try:
            return ZoneInfo(tz_name)
        except ZoneInfoNotFoundError:
            _tz_log.warning("display_timezone.unknown", tz_name=tz_name)
    local = datetime.now().astimezone().tzinfo
    if local is None:  # pragma: no cover — astimezone() always tags a tz
        return timezone.utc
    return local


def _coerce_datetime(value: datetime | str | None) -> datetime | None:
    """Normalise the input of :func:`_format_localtime`.

    Accepts a :class:`~datetime.datetime` (passed by routes via the
    Pydantic ``Screenshot`` model), an ISO-8601 string (in case a route
    hands the raw row through), or ``None``. Returns ``None`` on empty
    or unparseable input so the filter renders the same ``"—"``
    placeholder the legacy filters used.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        _tz_log.debug("localtime.parse.error", value=text[:40])
        return None


def _format_localtime(
    value: datetime | str | None,
    fmt: str = _LOCALTIME_DEFAULT,
) -> str:
    """Render a UTC ``captured_at`` in the operator's display timezone.

    Jinja filter registered as ``localtime``. ``fmt`` selects between
    the two supported strftime patterns (``"short"`` →  ``%H:%M``,
    ``"full"`` → ``%Y-%m-%d %H:%M``). Unknown ``fmt`` flags collapse to
    ``"short"`` so a template typo can never raise. Naive input
    datetimes are assumed to already represent UTC — the storage layer
    only ever writes UTC — and are tagged accordingly before conversion.
    """
    parsed = _coerce_datetime(value)
    if parsed is None:
        return "—"
    if parsed.tzinfo is None:
        # ``timezone.utc``, а НЕ ``ZoneInfo('UTC')``: на хостах без пакета
        # tzdata (uv-сборка CPython на Windows) ZoneInfo кидает
        # ZoneInfoNotFoundError и любая страница с наивной меткой времени
        # падала в 500. Значение то же самое, зависимости — никакой.
        parsed = parsed.replace(tzinfo=timezone.utc)
    pattern = _LOCALTIME_FORMATS.get(fmt, _LOCALTIME_FORMATS[_LOCALTIME_DEFAULT])
    return parsed.astimezone(resolve_display_tz()).strftime(pattern)


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

    Per-user (2026-08): для УЧАСТНИКА (не-владельца) читаем его строку в
    ``user_settings`` — глобальный ``kv_settings.theme`` принадлежит
    владельцу и раньше перекрашивал весь инстанс. Личность берём из
    :data:`app.request_ctx.current_member_uid` (её кладёт auth-гейт); нет
    строки — ДЕФОЛТНАЯ тема, а НЕ сохранённая владельцем.
    """
    uid = get_member_uid()
    if uid is not None:
        value = get_user_kv_sync(uid, "theme")
        if value is None or value.strip() not in _THEME_VALUES:
            return _THEME_DEFAULT
        return value.strip()
    value = _cached_kv_value("theme")
    if value is None or value not in _THEME_VALUES:
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
    _invalidate_kv_value("theme")


def _read_compact_from_db() -> str:
    """Synchronous read of the ``compact_mode`` row from ``kv_settings``.

    Mirrors :func:`_read_theme_from_db` — Jinja globals run synchronously
    so the aiosqlite pool is off-limits; a short stdlib ``sqlite3``
    reader against the WAL-mode database is safe alongside the async
    writers. Any failure (missing DB / row / bogus value) falls back to
    ``"0"`` so a template render never 500s because of this lookup.

    Участник (не-владелец) получает ДЕФОЛТ: строка ``compact_mode``
    глобальная (владельца), а per-user эта настройка пока не заведена —
    навязывать чужому аккаунту вид «как у владельца» неправильно.
    """
    if get_member_uid() is not None:
        return _COMPACT_DEFAULT
    value = _cached_kv_value("compact_mode")
    if value is None:
        return _COMPACT_DEFAULT
    value = value.strip()
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
    _invalidate_kv_value("compact_mode")


def _read_grayscale_from_db() -> str:
    """Synchronous read of the ``grayscale_mode`` row from ``kv_settings``.

    Mirrors :func:`_read_compact_from_db` — Jinja globals run
    synchronously so the aiosqlite pool is off-limits; a short stdlib
    ``sqlite3`` reader against the WAL-mode database is safe alongside
    the async writers. Any failure (missing DB / row / bogus value)
    falls back to ``"0"`` so a template render never 500s because of
    this lookup.

    Участник (не-владелец) получает ДЕФОЛТ — см. :func:`_read_compact_from_db`.
    """
    if get_member_uid() is not None:
        return _GRAYSCALE_DEFAULT
    value = _cached_kv_value("grayscale_mode")
    if value is None:
        return _GRAYSCALE_DEFAULT
    value = value.strip()
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
    _invalidate_kv_value("grayscale_mode")


def _read_reduce_motion_from_db() -> str:
    """Synchronous read of the ``reduce_motion`` row from ``kv_settings``.

    Mirrors :func:`_read_grayscale_from_db` — Jinja globals run
    synchronously so the aiosqlite pool is off-limits; a short stdlib
    ``sqlite3`` reader against the WAL-mode database is safe alongside
    the async writers. Any failure (missing DB / row / bogus value)
    falls back to ``"0"`` so a template render never 500s because of
    this lookup.

    Участник (не-владелец) получает ДЕФОЛТ — см. :func:`_read_compact_from_db`.
    """
    if get_member_uid() is not None:
        return _REDUCE_MOTION_DEFAULT
    value = _cached_kv_value("reduce_motion")
    if value is None:
        return _REDUCE_MOTION_DEFAULT
    value = value.strip()
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
    _invalidate_kv_value("reduce_motion")


templates.env.filters["humantime"] = _format_human_time
templates.env.filters["humandate"] = _format_human_date
templates.env.filters["clock"] = _format_clock
templates.env.filters["filesize"] = _format_filesize
templates.env.filters["thumbnail_url"] = _thumbnail_url
templates.env.filters["app_alias"] = _resolve_app_alias
templates.env.filters["linkify_urls"] = _linkify_urls
# v1.10 fix 2/3 — timezone-aware capture rendering. Pipe every
# ``captured_at`` (UTC in storage) through ``|localtime`` to convert into
# the operator's display zone before formatting. See
# :func:`_format_localtime` for the format flags.
templates.env.filters["localtime"] = _format_localtime

def _jinja_translate(key: str) -> str:
    """Jinja-facing wrapper around :func:`app.i18n.t`.

    Binds the ``lang`` argument to the active ``ui_language`` setting so
    templates can simply write ``{{ t("btn_save") }}`` without each
    route having to thread the language through context. The resolution
    uses the same per-request :class:`~contextvars.ContextVar` cache as
    every other ``kv_settings`` read on this page, so calling ``t(...)``
    on every line of :file:`base.html` triggers at most one SQLite hit
    per request.
    """
    return _translate(key, _get_ui_language())


def _read_kv_flag(key: str, default: str = "1") -> str:
    """Синхронное чтение «1»/«0» kv-флага из ``kv_settings`` (для Jinja-глобалов).

    Тот же безопасный паттерн, что ``_read_compact_from_db`` — короткое stdlib
    ``sqlite3`` чтение в WAL-режиме, любой сбой → ``default``.
    """
    value = _cached_kv_value(key)
    if value is None:
        return default
    value = value.strip()
    return value if value in ("0", "1") else default


def get_voice_default_on() -> str:
    """«1», если плавающая голосовая кнопка включена (деф. вкл). Голос по умолчанию."""
    return _read_kv_flag("voice_default_on", "1")


def get_ai_everywhere() -> str:
    """«1», если включён мастер-режим «ИИ везде» (деф. ВЫКЛ).

    Когда «1» — по всему сайту оживают ИИ-фичи (копилот справа снизу, ИИ-
    календарь, поиск настроек ИИ, саммари экранов). Дефолт «0» → сайт работает
    как обычно. Читается тем же безопасным sync-паттерном, что и остальные
    Jinja-флаги, поэтому доступен в ЛЮБОМ шаблоне без правки роутов.
    """
    return _read_kv_flag("ai_everywhere", "0")


templates.env.globals["get_ai_everywhere"] = get_ai_everywhere
templates.env.globals["get_voice_default_on"] = get_voice_default_on
templates.env.globals["get_theme"] = get_theme
templates.env.globals["get_compact_mode"] = get_compact_mode
templates.env.globals["get_grayscale_mode"] = get_grayscale_mode
templates.env.globals["get_reduce_motion"] = get_reduce_motion
templates.env.globals["get_ui_language"] = _get_ui_language
templates.env.globals["t"] = _jinja_translate
# v1.0 capstone 3/3 — expose the package version to every template so
# :file:`base.html` can stamp the version-banner chip without each route
# having to thread the value through context. Reads ``app.__version__``
# once at import time; the value is immutable for the lifetime of the
# process.
templates.env.globals["app_version"] = _app_version


def _csrf_input_global(request: object = None) -> object:
    """CSRF-поле для формы. Зарегистрирован ЗДЕСЬ, а не только в ``create_app``.

    ``base.html`` зовёт ``csrf_input(request)``, поэтому любой рендер мимо
    приложения (тесты, офлайн-генерация письма) падал с ``UndefinedError``.
    Глобалы шаблонов живут в этом модуле — значит и этот тоже. Импорт ленивый:
    ``app.web.middleware.csrf`` тянет настройки, а этот модуль импортируется
    очень рано.
    """
    from app.web.middleware.csrf import csrf_input  # noqa: PLC0415

    return csrf_input(request)


def _csrf_token_global(request: object = None) -> str:
    """Голый CSRF-токен (для ``hx-headers`` и fetch). См. ``_csrf_input_global``."""
    from app.web.middleware.csrf import csrf_token_for_request  # noqa: PLC0415

    return csrf_token_for_request(request)


templates.env.globals["csrf_input"] = _csrf_input_global
templates.env.globals["csrf_token"] = _csrf_token_global
