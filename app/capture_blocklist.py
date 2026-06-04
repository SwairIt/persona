"""Regex-based capture blocklist (v1.21).

Stricter sibling of :mod:`app.app_capture_skip`. That module matches
exact normalised app names; this one matches arbitrary regular
expressions against either the active window's ``app_name`` or its
``window_title`` (or both). Power users want patterns like ``Bank`` in
the title or ``KeePass.*`` in the app name, neither of which a flat
exact-match list can express.

Rule shape (``capture_regex_blocklist`` table):
    * ``pattern``  — raw regex source.
    * ``field``    — one of ``"app"``, ``"title"``, ``"both"``.
    * ``enabled``  — soft toggle, only ``1`` rows are loaded.

Hot path: :func:`is_blocked` runs once per capture iteration from
:mod:`app.workers.capture_loop`. To keep that path cheap we compile
each rule's regex once and stash the result in a module-level cache
keyed by a SHA-256 fingerprint of the currently-enabled patterns.
Callers re-fetch the rules every iteration via
:func:`list_active_rules` (cheap indexed SQLite read), and the
compile-and-cache step inside :func:`list_active_rules` is a no-op as
long as the fingerprint matches.

Matching is case-insensitive (``re.IGNORECASE``); patterns are run
through :meth:`re.Pattern.search`, so an operator does not need to
anchor with ``.*`` to match a substring. A pattern that fails to
compile is logged at WARNING and silently dropped from the active set
— a single bad rule must never break the rest of the blocklist or
nuke the capture loop.
"""

from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING, Literal

from app.logging_setup import get_logger

if TYPE_CHECKING:
    import aiosqlite

log = get_logger("persona.capture_blocklist")

# Valid values for the ``field`` column. Kept as a literal type so
# mypy --strict catches a stray string in a caller; the storage-level
# CHECK constraint enforces the same set at write time.
FieldName = Literal["app", "title", "both"]

_VALID_FIELDS: frozenset[str] = frozenset({"app", "title", "both"})

# Module-level compile cache. ``_cache`` holds the fingerprint of the
# current set of enabled patterns plus the compiled rule list keyed by
# that fingerprint; on a fingerprint mismatch the list is recompiled
# from scratch. ``None`` means "no rules ever loaded yet" — first call
# always re-compiles.
_CompiledRule = tuple[re.Pattern[str], FieldName]
_cache: tuple[str, list[_CompiledRule]] | None = None


def _fingerprint(patterns: list[str]) -> str:
    """SHA-256 of every enabled pattern, sorted for stability.

    Sorting means the cache is insensitive to row-order changes from
    SQLite (which has no guaranteed ordering without ``ORDER BY`` on
    arbitrary columns) — only the *set* of enabled patterns matters
    for whether a recompile is needed.
    """
    joined = "\n".join(sorted(patterns))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def _compile_rules(rows: list[tuple[str, str]]) -> list[_CompiledRule]:
    """Compile ``[(pattern, field), ...]`` to ``[(re.Pattern, field), ...]``.

    Patterns that fail to compile are logged and skipped — see module
    docstring. ``re.IGNORECASE`` is baked in here so callers never have
    to remember the flag.
    """
    compiled: list[_CompiledRule] = []
    for pattern, field in rows:
        if field not in _VALID_FIELDS:
            log.warning(
                "capture_blocklist.invalid_field",
                pattern=pattern,
                field=field,
            )
            continue
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            log.warning(
                "capture_blocklist.bad_pattern",
                pattern=pattern,
                error=str(exc),
            )
            continue
        # Cast through Literal for mypy --strict: ``field`` is ``str``
        # at runtime but we just verified it is one of the three valid
        # values, so the runtime invariant holds.
        compiled.append((regex, field))  # type: ignore[arg-type]
    return compiled


async def list_active_rules(
    conn: aiosqlite.Connection,
) -> list[_CompiledRule]:
    """Return every enabled rule, compiled and cached.

    Pulls ``pattern, field`` from the table where ``enabled=1``,
    re-uses the module-level compile cache when the fingerprint of the
    pattern set is unchanged, and compiles from scratch otherwise. The
    caller (the capture loop) calls this every iteration; the SQLite
    read is indexed and cheap, and the compile is amortised across all
    iterations during which the rule set stays unchanged.
    """
    global _cache  # noqa: PLW0603 — single module-wide cache by design

    cursor = await conn.execute(
        "SELECT pattern, field FROM capture_regex_blocklist "
        "WHERE enabled = 1 ORDER BY id"
    )
    rows = await cursor.fetchall()
    raw: list[tuple[str, str]] = [
        (str(row["pattern"]), str(row["field"])) for row in rows
    ]
    fingerprint = _fingerprint([pattern for pattern, _field in raw])

    if _cache is not None and _cache[0] == fingerprint:
        return _cache[1]

    compiled = _compile_rules(raw)
    _cache = (fingerprint, compiled)
    log.debug(
        "capture_blocklist.recompiled",
        rule_count=len(compiled),
        fingerprint=fingerprint[:12],
    )
    return compiled


def is_blocked(
    active_app: str | None,
    window_title: str | None,
    rules: list[_CompiledRule],
) -> bool:
    """Return ``True`` when any rule matches the active window.

    Pure synchronous function — no DB I/O, no logging on the hot path
    (the caller logs the matched pattern once it knows we're blocking).
    Both inputs may be ``None`` (the foreground-window probe sometimes
    hands back ``None`` for shell surfaces); a rule whose target field
    is ``None`` simply does not match, but other fields still get
    checked. Returns ``True`` on the first matching rule — ordering is
    therefore irrelevant for correctness, only for the field reported
    by an upstream debug log.
    """
    if not rules:
        return False
    app_value = active_app or ""
    title_value = window_title or ""
    for regex, field in rules:
        if field in ("app", "both") and app_value and regex.search(app_value):
            return True
        if field in ("title", "both") and title_value and regex.search(title_value):
            return True
    return False


def find_matching_rule(
    active_app: str | None,
    window_title: str | None,
    rules: list[_CompiledRule],
) -> _CompiledRule | None:
    """Return the first rule that matches, or ``None``.

    Companion to :func:`is_blocked` for callers that want to log
    *which* pattern triggered the block. Kept separate so the hot
    ``is_blocked`` path stays branch-light when no caller needs the
    detail.
    """
    if not rules:
        return None
    app_value = active_app or ""
    title_value = window_title or ""
    for rule in rules:
        regex, field = rule
        if field in ("app", "both") and app_value and regex.search(app_value):
            return rule
        if field in ("title", "both") and title_value and regex.search(title_value):
            return rule
    return None


async def list_rules(conn: aiosqlite.Connection) -> list[aiosqlite.Row]:
    """Return every rule (enabled + disabled) for the admin UI."""
    cursor = await conn.execute(
        "SELECT id, pattern, field, enabled, created_at, description "
        "FROM capture_regex_blocklist ORDER BY id DESC"
    )
    return list(await cursor.fetchall())


async def add_rule(
    conn: aiosqlite.Connection,
    *,
    pattern: str,
    field: str,
    description: str | None,
) -> int:
    """Insert a new rule. Raises ``ValueError`` on bad input.

    Validates that ``pattern`` is non-empty and compiles cleanly, and
    that ``field`` is one of the three accepted values. Returns the
    newly assigned ``id`` so the caller can redirect or log.
    """
    pattern_clean = pattern.strip()
    if not pattern_clean:
        msg = "pattern is required"
        raise ValueError(msg)
    if field not in _VALID_FIELDS:
        msg = f"field must be one of app/title/both, got {field!r}"
        raise ValueError(msg)
    try:
        re.compile(pattern_clean)
    except re.error as exc:
        msg = f"invalid regex: {exc}"
        raise ValueError(msg) from exc

    description_clean = (description or "").strip() or None
    cursor = await conn.execute(
        "INSERT INTO capture_regex_blocklist (pattern, field, description) "
        "VALUES (?, ?, ?)",
        (pattern_clean, field, description_clean),
    )
    await conn.commit()
    new_id = int(cursor.lastrowid or 0)
    log.info(
        "capture_blocklist.added",
        rule_id=new_id,
        pattern=pattern_clean,
        field=field,
    )
    invalidate_cache()
    return new_id


async def delete_rule(conn: aiosqlite.Connection, rule_id: int) -> None:
    """Remove ``rule_id``. Idempotent — missing rows are fine."""
    await conn.execute(
        "DELETE FROM capture_regex_blocklist WHERE id = ?",
        (rule_id,),
    )
    await conn.commit()
    log.info("capture_blocklist.deleted", rule_id=rule_id)
    invalidate_cache()


async def toggle_rule(conn: aiosqlite.Connection, rule_id: int) -> None:
    """Flip the ``enabled`` flag for ``rule_id``. No-op when missing."""
    await conn.execute(
        "UPDATE capture_regex_blocklist SET enabled = 1 - enabled WHERE id = ?",
        (rule_id,),
    )
    await conn.commit()
    log.info("capture_blocklist.toggled", rule_id=rule_id)
    invalidate_cache()


def invalidate_cache() -> None:
    """Drop the compile cache. Called from every write path.

    Defensive: the cache also self-invalidates on fingerprint mismatch
    inside :func:`list_active_rules`, so this is a belt-and-braces
    nudge for the case where the next reader hits the cache before the
    DB row is observable through the read-replica view — never seen on
    SQLite in WAL mode but cheap to guarantee.
    """
    global _cache  # noqa: PLW0603 — single module-wide cache by design
    _cache = None


__all__ = [
    "FieldName",
    "add_rule",
    "delete_rule",
    "find_matching_rule",
    "invalidate_cache",
    "is_blocked",
    "list_active_rules",
    "list_rules",
    "toggle_rule",
]
