"""Privacy-mode sentinel detector for the capture loop (v1.40).

Stricter sibling of :mod:`app.capture_blocklist`. The regex blocklist
still emits a ``capture.blocked_by_regex`` log line that names the
matched pattern AND the first 80 chars of the window title; that is
fine for "do not screenshot this banking site" but leaks enough
context for an over-the-shoulder audit to learn that the user was
*looking at* a particular site at a given moment.

Privacy mode is the no-trace path:

* The capture loop short-circuits BEFORE any metadata row is written
  (no ``screenshots`` insert, no thumbnail, no OCR queue entry).
* The only artefact is a row in ``privacy_skip_event`` whose
  ``window_title_hash`` is the first 16 chars of the SHA-256 of the
  raw title — enough to prove a skip happened against a recurring
  context, useless for reconstructing the original text.

Patterns are matched case-insensitively as substrings, against either
``app_name`` or ``window_title``. Substring match (not regex) keeps
the hot path branch-light and the sentinel list readable: the operator
opens this file, not the database, to learn what is shielded.

To DISABLE the feature globally, set the ``PRIVACY_PATTERNS`` tuple to
``()`` and re-deploy — there is intentionally no kv toggle, because a
toggle that an attacker can flip via the admin UI would defeat the
purpose of a privacy mode.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

from app.logging_setup import get_logger
from app.storage.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.privacy_mode")


# Sentinel patterns. Substring, case-insensitive — see module docstring.
# Each entry is a raw regex source string for forward-compatibility,
# but every shipped pattern is in fact a literal substring; if you add
# a true regex, remember the alternation operator works (``a|b``) but
# anchors do not (we use :meth:`re.Pattern.search`).
PRIVACY_PATTERNS: tuple[str, ...] = (
    r"Incognito",
    r"InPrivate",
    r"Private Browsing",
    r"Private Window",
    r"KeePass",
    r"1Password",
    r"Bitwarden",
    r"LastPass",
    r"Dashlane",
    r"Bank of",
    r"Сбербанк",
    r"Тинькофф",
    r"Tinkoff",
    r"banking",
    r"Online Banking",
)


# Compiled hard-coded patterns. Stable across the lifetime of the
# process: the source tuple is a module literal, so this never needs
# invalidation. ``re.IGNORECASE`` is baked in so callers cannot forget.
_HARDCODED_COMPILED: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(p, re.IGNORECASE), p) for p in PRIVACY_PATTERNS
)


# v1.43 — DB-backed compile cache mirroring :mod:`app.capture_blocklist`.
# ``_active_cache`` is ``(fingerprint, compiled_rules)`` for the latest
# observed set of bundle patterns. ``None`` means "no rules ever loaded
# yet" → the next call to :func:`refresh_active_patterns_cache` will
# recompile from scratch. Sync :func:`is_private_window` consults the
# cached compiled rules without doing I/O, falling back to the
# hard-coded set on a DB hiccup (see :data:`PRIVACY_PATTERNS` docstring
# for why the floor is non-negotiable).
_CompiledRule = tuple[re.Pattern[str], str]
_active_cache: tuple[str, list[_CompiledRule]] | None = None


def _fingerprint(patterns: list[str]) -> str:
    """SHA-256 of every active pattern, sorted for stability.

    Sorting means the cache is insensitive to row-order changes from
    SQLite — only the *set* of active patterns matters for whether a
    recompile is needed. Mirrors :func:`app.capture_blocklist._fingerprint`.
    """
    joined = "\n".join(sorted(patterns))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _compile_patterns(patterns: list[str]) -> list[_CompiledRule]:
    """Compile ``[pattern, ...]`` to ``[(re.Pattern, source), ...]``.

    Patterns that fail to compile are logged at WARNING and silently
    dropped from the active set — a single bad pattern must never
    nuke privacy mode. ``re.IGNORECASE`` is baked in here so callers
    never have to remember the flag.
    """
    compiled: list[_CompiledRule] = []
    for pattern in patterns:
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            log.warning(
                "privacy_mode.bad_pattern",
                pattern=pattern,
                error=str(exc),
            )
            continue
        compiled.append((regex, pattern))
    return compiled


async def refresh_active_patterns_cache() -> list[_CompiledRule]:
    """Re-read DB bundles + hard-coded floor, update the compile cache.

    Returns the freshly-cached compiled rule list. Hot-path callers
    (capture-loop) may invoke this once per iteration; the SQLite
    read is indexed and cheap, and the fingerprint short-circuit
    skips recompilation when nothing changed. Write paths in the
    admin UI call :func:`invalidate_active_patterns_cache` after each
    mutation so the next reader sees the fresh set.
    """
    global _active_cache  # noqa: PLW0603 — single module-wide cache by design

    # Lazy import to keep the dependency one-way: ``privacy_bundles``
    # imports from this module (for the hard-coded floor and the
    # invalidate hook), so we cannot import it at top level.
    from app.privacy_bundles import list_active_patterns  # noqa: PLC0415

    patterns = await list_active_patterns()
    fingerprint = _fingerprint(patterns)

    if _active_cache is not None and _active_cache[0] == fingerprint:
        return _active_cache[1]

    compiled = _compile_patterns(patterns)
    _active_cache = (fingerprint, compiled)
    log.debug(
        "privacy_mode.recompiled",
        pattern_count=len(compiled),
        fingerprint=fingerprint[:12],
    )
    return compiled


def invalidate_active_patterns_cache() -> None:
    """Drop the compile cache. Called from every bundle write path.

    Defensive belt-and-braces: the cache also self-invalidates on
    fingerprint mismatch inside :func:`refresh_active_patterns_cache`,
    so this is a nudge for callers that want the next reader to
    observe a fresh set without waiting for that fingerprint check.
    """
    global _active_cache  # noqa: PLW0603 — single module-wide cache by design
    _active_cache = None


def _resolve_compiled() -> tuple[tuple[re.Pattern[str], str], ...]:
    """Return the compiled rule set, DB-cache first then hard-coded.

    Sync helper for :func:`is_private_window`. When the DB cache has
    been warmed (the admin UI installed a preset, or the capture
    loop ran :func:`refresh_active_patterns_cache` at least once),
    those rules already include the hard-coded floor — see
    :func:`app.privacy_bundles.list_active_patterns`. Otherwise we
    fall back to the import-time hard-coded compile so privacy mode
    keeps working on a cold start before the first refresh.
    """
    if _active_cache is not None:
        cached = _active_cache[1]
        if cached:
            return tuple(cached)
    return _HARDCODED_COMPILED


def is_private_window(
    app_name: str | None,
    window_title: str | None,
) -> tuple[bool, str | None]:
    """Return ``(matched, pattern_source)`` for the active window.

    Pure function — no I/O, safe to call from the hot path. Both
    inputs may be ``None`` (the foreground-window probe sometimes
    returns ``None`` on the lock screen or for shell surfaces); in
    that case nothing matches and we return ``(False, None)``.

    v1.43 update: also consults the user-editable bundle patterns
    via the DB-backed compile cache (see
    :func:`refresh_active_patterns_cache`). The hard-coded
    :data:`PRIVACY_PATTERNS` tuple remains the back-compat floor when
    the DB read has not happened yet or fails.

    The second element of the tuple is the *raw pattern source* (the
    string the operator typed), not the compiled regex — so the
    caller can persist it without depending on this module's compile
    cache.
    """
    compiled = _resolve_compiled()
    if not compiled:
        return (False, None)
    app_value = app_name or ""
    title_value = window_title or ""
    if not app_value and not title_value:
        return (False, None)
    for regex, source in compiled:
        if app_value and regex.search(app_value):
            return (True, source)
        if title_value and regex.search(title_value):
            return (True, source)
    return (False, None)


def _hash_title(window_title: str | None) -> str | None:
    """Return the first 16 hex chars of SHA-256(title), or ``None``.

    Truncating to 16 chars (64 bits) keeps the audit log compact while
    still leaving enough entropy to distinguish recurring contexts.
    ``None`` in → ``None`` out: there is no value in hashing the empty
    string (every empty-title skip would collapse to the same hash).
    """
    if not window_title:
        return None
    digest = hashlib.sha256(window_title.encode("utf-8")).hexdigest()
    return digest[:16]


async def record_skip(
    app_name: str | None,
    window_title: str | None,
    pattern: str,
) -> None:
    """Persist a single privacy-mode skip event.

    We never store ``window_title`` text — only its truncated hash.
    Failure modes (DB locked, transient I/O) are swallowed and logged
    at DEBUG, because a broken audit log must not silently stop the
    capture loop. The skip itself has already happened upstream.
    """
    title_hash = _hash_title(window_title)
    try:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO privacy_skip_event "
                "(pattern_matched, app_name, window_title_hash) "
                "VALUES (?, ?, ?)",
                (pattern, app_name, title_hash),
            )
            await conn.commit()
    except Exception as exc:
        log.debug("privacy_mode.record_failed", error=str(exc))
        return
    log.info(
        "privacy_mode.skipped",
        pattern=pattern,
        app=app_name,
        title_hash=title_hash,
    )


async def stats(conn: aiosqlite.Connection) -> dict[str, object]:
    """Return ``{today, last7d, by_pattern}`` counters for the admin UI.

    All counters are simple ``COUNT(*)`` against the indexed
    ``skipped_at`` column. ``by_pattern`` is the last-7-day breakdown
    grouped by ``pattern_matched`` so the operator can see which
    sentinel is firing most often.
    """
    today_cur = await conn.execute(
        "SELECT COUNT(*) FROM privacy_skip_event "
        "WHERE skipped_at >= date('now')"
    )
    today_row = await today_cur.fetchone()
    today_count = int(today_row[0]) if today_row is not None else 0

    week_cur = await conn.execute(
        "SELECT COUNT(*) FROM privacy_skip_event "
        "WHERE skipped_at >= datetime('now', '-7 days')"
    )
    week_row = await week_cur.fetchone()
    week_count = int(week_row[0]) if week_row is not None else 0

    by_pattern_cur = await conn.execute(
        "SELECT pattern_matched, COUNT(*) AS n FROM privacy_skip_event "
        "WHERE skipped_at >= datetime('now', '-7 days') "
        "GROUP BY pattern_matched ORDER BY n DESC"
    )
    by_pattern_rows = await by_pattern_cur.fetchall()
    by_pattern: list[dict[str, object]] = [
        {
            "pattern": (
                str(row["pattern_matched"])
                if row["pattern_matched"] is not None
                else None
            ),
            "count": int(row["n"]),
        }
        for row in by_pattern_rows
    ]

    return {
        "today": today_count,
        "last7d": week_count,
        "by_pattern": by_pattern,
    }


__all__ = [
    "PRIVACY_PATTERNS",
    "invalidate_active_patterns_cache",
    "is_private_window",
    "record_skip",
    "refresh_active_patterns_cache",
    "stats",
]
